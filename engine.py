"""Game rules, Telegram update routing and topic isolation.

All public effects go through public(): no fallback to General.
Handlers run within a Store transaction, including score/history/outbox writes.
"""
import html
import random
import secrets
import time
from collections import Counter

from bank import Bank


def mention(player):
    return f'<a href="tg://user?id={player["uid"]}">{html.escape(player["name"])}</a>'


def keys(rows):
    return {'inline_keyboard': rows}


def button(text, data):
    return {'text': text, 'callback_data': data}


def schedule(players, rounds, rng):
    """Random mutual pairs; every player participates in each lap.

    An odd leftover pairs with a random partner (one extra turn).
    Pair repeats are avoided when possible, never self-pairs.
    """
    result, used_pairs = [], set()
    ids = [p['uid'] for p in players]
    for _ in range(rounds):
        candidates = []
        for _ in range(40):
            order = ids[:]
            rng.shuffle(order)
            pairs = [(order[i], order[i + 1]) for i in range(0, len(order) - 1, 2)]
            if len(order) % 2:
                partners = [x for x in order[:-1]
                            if tuple(sorted((x, order[-1]))) not in used_pairs]
                pairs.append((order[-1], rng.choice(partners or order[:-1])))
            repeats = sum(tuple(sorted(p)) in used_pairs for p in pairs)
            candidates.append((repeats, pairs))
            if not repeats:
                break
        pairs = min(candidates, key=lambda x: x[0])[1]
        rng.shuffle(pairs)
        for a, b in pairs:
            used_pairs.add(tuple(sorted((a, b))))
            if rng.randrange(2):
                a, b = b, a
            result.extend([[a, b], [b, a]])
    return result


def schedule_count(players, count, rng):
    """Exact even question count, retaining both directions of each pair."""
    if count < 4 or count > 40 or count % 2:
        raise ValueError('Question count must be even, 4–40.')
    per_lap = len(players) + len(players) % 2
    rounds = (count + per_lap - 1) // per_lap
    return schedule(players, rounds, rng)[:count]


HELP = '''🎭 برا السالفة — النسخة العراقية
داخل قسم اللعبة:
/game — افتح كيم وانضموا من الرابط
/settings — عرض إعدادات الكروب وأزرار التعديل للأدمن
/settings 35 12 — حفظ وقت السؤال وعدد الأسئلة، للأدمن فقط
/status — حالة الكيم
/scores — نقاط هذا الكروب
/cancel — إلغاء، للمضيف أو أدمن
/resetwords c01 — إعادة تصنيف انتهت كلماته، للأدمن وبتأكيد

بالخاص:
/guess الكلمة — محاولة واحدة للي برا أثناء الأسئلة
/leave — اطلع من اللوبي قبل توزيع الأدوار

الأسئلة والأجوبة بنفس قسم الكروب. الأدوار والتصويت بالخاص.
التصويت ممنوع لنفسك؛ عدم التصويت والتعادل ينجّيان اللي برا.
اللي برا إذا نجا ياخذ ٣ نقاط. إذا انكشف وخمّن صح: إله ٢ ولكل مصوّت صحيح ١.
إذا انكشف وغلط: لكل مصوّت صحيح ٢. التخمين المبكر الصحيح إله ٣.
التخمين المبكر الغلط يعطي كل اللي داخل نقطتين وينهي الكيم.
كتابة الكلمة أو اسم بديل إلها من اللي برا أثناء الأسئلة تعتبر تخمين صحيح.
إذا اللي داخل كشف الكلمة، تنلغي الجولة بدون نقاط.
للأدمن: /bind داخل قسم مخصص غير العام لربط البوت بيه.'''


class Engine:
    def __init__(self, store, username, authorize, bank=None, rng=None, clock=None):
        self.s = store
        self.username = username
        self.authorize = authorize  # (chat, uid, admin=False) -> bool
        self.bank = bank or Bank()
        self.rng = rng or random.SystemRandom()
        self.now = clock or time.time

    def dm(self, uid, text, markup=None, game=None, essential=False):
        payload = {'chat_id': uid, 'text': text, 'parse_mode': 'HTML', 'protect_content': True}
        if markup:
            payload['reply_markup'] = markup
        self.s.enqueue('sendMessage', payload, game['id'] if game else None, essential)

    def public(self, game, text, markup=None):
        # Always include the dedicated topic id, including results and timeouts.
        payload = {'chat_id': game['chat'], 'message_thread_id': game['topic'],
                   'text': text, 'parse_mode': 'HTML'}
        if markup:
            payload['reply_markup'] = markup
        self.s.enqueue('sendMessage', payload, game.get('id'), True)

    def player(self, game, uid):
        return next((p for p in game['players'] if p['uid'] == uid), None)

    def ack(self, callback, text='تم', alert=False):
        self.s.enqueue('answerCallbackQuery', {'callback_query_id': callback['id'],
                                              'text': text, 'show_alert': alert})

    def lobby(self, game):
        link = f'https://t.me/{self.username}?start=j_{game["id"]}'
        names = '\n'.join(f'• {mention(p)}' for p in game['players']) or 'بعد ماكو لاعبين'
        self.public(game, f'🎭 لوبي برا السالفة\n{names}\n\n٣–١٢ لاعب. كل واحد يفتح رابط الانضمام ويضغط Start.\n'
                    f'الوقت: {game["seconds"]} ثانية لكل سؤال وجواب، عدد الأسئلة: {game["question_count"]}.',
                    keys([[{'text': '🎮 انضم وافتح الخاص', 'url': link}],
                          [button('🎲 اختار التصنيف وابدأ', f'cats:{game["id"]}')],
                          [button('إلغاء الكيم', f'cancel:{game["id"]}')]]))

    def new_game(self, msg):
        chat, topic = msg['chat']['id'], msg.get('message_thread_id', 0)
        if self.s.active(chat):
            self.public(self.s.active(chat), 'أكو كيم مفتوح. استخدم /status أو /cancel.')
            return
        settings = self.s.settings(chat)
        game = {'id': secrets.token_hex(6), 'chat': chat, 'topic': topic,
                'host': msg['from']['id'], 'stage': 'lobby', 'players': [],
                'seconds': settings['seconds'], 'question_count': settings['question_count'], 'deadline': self.now() + 900,
                'ready': [], 'votes': {}, 'guess_used': False}
        self.s.save(game)
        self.lobby(game)

    def join(self, rid, user):
        game = self.s.game(rid)
        uid = user['id']
        if not game or game['stage'] != 'lobby' or game['deadline'] <= self.now():
            self.dm(uid, 'هذا اللوبي انتهى أو الكيم بدأ. افتح رابط الكيم الجديد.')
            return
        if not self.authorize(game['chat'], uid):
            self.dm(uid, 'لازم تكون عضو بالكروب حتى تنضم.')
            return
        for other in self.s.active_games():
            if other['id'] != rid and self.player(other, uid):
                self.dm(uid, 'أنت مشارك بكيم ثاني. كمّله أولاً، أو /leave إذا بعده لوبي.')
                return
        existing = self.player(game, uid)
        if not existing and len(game['players']) >= 12:
            self.dm(uid, 'الكيم ممتلئ، الحد ١٢ لاعب.')
            return
        if not existing:
            game['players'].append({'uid': uid, 'name': user.get('first_name', 'لاعب')[:50]})
            self.s.save(game)
            self.lobby(game)
        self.dm(uid, 'انضمّيت ✅ الدور والسالفة يوصلولك هنا. لا تحظر البوت أثناء الكيم.', game=game)

    def categories(self, game):
        categories = self.bank.categories[:]
        self.rng.shuffle(categories)
        used = self.s.used(game['chat'])
        rows = [[button(f'{c["name"]} ({len(self.bank.remaining(c["id"], used))})',
                        f'cat:{game["id"]}:{c["id"]}')] for c in categories]
        self.public(game, 'المضيف يختار التصنيف. الرقم هو السوالف الباقية بدون تكرار. الترتيب عشوائي 🎲', keys(rows))

    def show_settings(self, context, changed=False):
        cfg = self.s.settings(context['chat'])
        prefix = f'cfg:{context["chat"]}:{context["topic"]}'
        rows = [
            [button('⏱ أقل ٥ ثواني', prefix + ':seconds:-5'),
             button('⏱ أكثر ٥ ثواني', prefix + ':seconds:5')],
            [button('❓ أقل سؤالين', prefix + ':question_count:-2'),
             button('❓ أكثر سؤالين', prefix + ':question_count:2')],
        ]
        title = '✅ حفظت إعدادات الكروب' if changed else '⚙️ إعدادات هذا الكروب'
        self.public(context, f'{title}\nوقت السؤال وجوابه: {cfg["seconds"]} ثانية\n'
                    f'عدد الأسئلة قبل التصويت: {cfg["question_count"]}\n\n'
                    'الأدمن فقط يگدر يعدّل. تنحفظ بعد إعادة التشغيل وتطبّق على الكيم الجديد، حتى اللوبي الحالي يحتفظ بإعداداته.\n'
                    'الوقت ٢٠–٩٠ ثانية؛ عدد الأسئلة زوجي من ٤ إلى ٤٠ حتى الطرفين يسألون بعض.\n'
                    'تگدر تستخدم مثلاً /settings 45 16', keys(rows))

    def begin(self, game, category):
        if category not in self.bank.by_category or len(game['players']) < 3:
            return False
        missing = [p for p in game['players'] if not self.authorize(game['chat'], p['uid'])]
        if missing:
            game['players'] = [p for p in game['players'] if p not in missing]
            self.s.save(game)
            self.public(game, 'بعض اللاعبين طلعوا من الكروب، شلتهم من اللوبي. اختاروا التصنيف مجدداً.')
            return False
        word = self.bank.choose(category, self.s.used(game['chat']), self.rng)
        if not word:
            self.public(game, 'خلصت كلمات هذا التصنيف. اختاروا غيره؛ الأدمن يگدر يعيده بـ /resetwords ' + category)
            return False
        self.s.db.execute('INSERT INTO used VALUES(?,?)', (game['chat'], word['id']))
        game.update(stage='ready', category=category, word=word['id'],
                    outsider=self.rng.choice(game['players'])['uid'], ready=[],
                    turns=schedule_count(game['players'], game['question_count'], self.rng),
                    turn=0, waiting='question', deadline=self.now() + 120)
        self.s.save(game)
        name = html.escape(self.bank.by_category[category]['name'])
        for p in game['players']:
            role = ('أنت <b>برا السالفة</b> 🕵️\nما تعرف الكلمة. رقّع وخمّن من الأجوبة، أو /guess الكلمة بمحاولة واحدة.'
                    if p['uid'] == game['outsider'] else
                    f'أنت <b>داخل السالفة</b> ✅\nالكلمة: <b>{html.escape(word["text"])}</b>\nلا تكتبها بالكروب ولا تفضحها.')
            self.dm(p['uid'], f'التصنيف: {name}\n{role}\n\nمن تقرأ دورك اضغط جاهز.',
                    keys([[button('✅ قريت دوري وجاهز', f'ready:{game["id"]}')]]), game, True)
        self.public(game, f'التصنيف: <b>{name}</b>\nالأدوار وصلت بالخاص؛ اضغطوا جاهز خلال دقيقتين.\n'
                    f'عدد الأسئلة: {len(game["turns"])}، الوقت لكل سؤال وجواب: {game["seconds"]} ثانية.')
        return True

    def prompt(self, game):
        if game['turn'] >= len(game['turns']):
            self.start_vote(game)
            return
        a, b = game['turns'][game['turn']]
        game.update(stage='questions', waiting='question', deadline=self.now() + game['seconds'])
        self.s.save(game)
        self.public(game, f'❓ السؤال {game["turn"] + 1}/{len(game["turns"])}\n'
                    f'{mention(self.player(game, a))} اسأل {mention(self.player(game, b))}.\n'
                    f'عندكم {game["seconds"]} ثانية للسؤال والجواب. اكتب السؤال كنص، وبعده الطرف الثاني يجاوب.\n'
                    'السؤال الجاي تتبادلون الأدوار؛ لا تذكرون الكلمة!')

    def advance(self, game, timed_out=False):
        if timed_out:
            self.public(game, '⏰ خلص وقت هذا السؤال. نكمل.')
        game['turn'] += 1
        self.prompt(game)

    def start_vote(self, game):
        game.update(stage='voting', votes={}, deadline=self.now() + 60)
        self.s.save(game)
        for p in game['players']:
            rows = [[button(q['name'], f'vote:{game["id"]}:{q["uid"]}')]
                    for q in game['players'] if q['uid'] != p['uid']]
            self.dm(p['uid'], '🗳 منو برا السالفة؟ صوّت خلال دقيقة. صوتك سري وتگدر تغيّره لحد الإغلاق.',
                    keys(rows), game, True)
        self.public(game, '🗳 خلصت الأسئلة. التصويت وصل بالخاص؛ عندكم دقيقة. ننتظر نهاية الوقت حتى الكل يگدر يغيّر صوته.')

    def close_vote(self, game):
        counts = Counter(game['votes'].values())
        high = max(counts.values(), default=0)
        leaders = [uid for uid, count in counts.items() if count == high]
        game['vote_counts'] = dict(counts)
        if len(leaders) == 1 and leaders[0] == game['outsider']:
            game.update(stage='guess', deadline=self.now() + 30)
            self.s.save(game)
            self.dm(game['outsider'], 'انكشف دورك! عندك ٣٠ ثانية ومحاولة واحدة. اكتب الكلمة هنا، أو /guess الكلمة.',
                    game=game, essential=True)
            self.public(game, f'انكشف {mention(self.player(game, game["outsider"]))}! عنده تخمين أخير بالخاص خلال ٣٠ ثانية.')
        else:
            reason = 'التصويت تعادل أو ماكو أصوات.' if len(leaders) != 1 else 'صوّتّوا على شخص ثاني.'
            self.finish(game, 'escaped', reason + ' اللي برا نجا وياخذ ٣ نقاط.')

    def guess(self, game, uid, text, explicit=False):
        if uid != game.get('outsider') or game['stage'] not in ('questions', 'guess'):
            self.dm(uid, 'التخمين متاح فقط للي برا أثناء الأسئلة أو بمحاولته الأخيرة.')
            return
        if game['guess_used']:
            self.dm(uid, 'استخدمت محاولتك.')
            return
        game['guess_used'] = True
        correct = self.bank.matches(self.bank.by_word[game['word']], text)
        if game['stage'] == 'guess':
            self.finish(game, 'caught_correct' if correct else 'caught_wrong',
                        'اللي برا خمّن صح بعد التصويت!' if correct else 'التخمين الأخير غلط.')
        else:
            self.finish(game, 'early_correct' if correct else 'early_wrong',
                        'اللي برا عرف الكلمة قبل التصويت!' if correct else 'اللي برا خمّن قبل التصويت وغلط.')

    def finish(self, game, result, reason):
        if game['stage'] == 'ended':
            return
        # Remove stale roles/prompts/votes when a round ends, including failed retries.
        self.s.db.execute('DELETE FROM outbox WHERE game=?', (game['id'],))
        gains = {p['uid']: 0 for p in game['players']}
        if result in ('escaped', 'early_correct'):
            gains[game['outsider']] = 3
        elif result == 'early_wrong':
            for uid in gains:
                if uid != game['outsider']:
                    gains[uid] = 2
        elif result in ('caught_correct', 'caught_wrong'):
            if result == 'caught_correct':
                gains[game['outsider']] = 2
            for voter, target in game['votes'].items():
                if target == game['outsider']:
                    gains[int(voter)] = 1 if result == 'caught_correct' else 2
        for p in game['players']:
            self.s.add_score(game['chat'], p, gains[p['uid']])
        game.update(stage='ended', result=result, deadline=0, ended_at=self.now())
        self.s.save(game)
        summary = html.escape(reason)
        if game.get('word'):
            word = self.bank.by_word[game['word']]
            summary += (f'\nالسالفة: <b>{html.escape(word["text"])}</b>\n'
                        f'اللي برا: {mention(self.player(game, game["outsider"]))}')
        if game.get('vote_counts'):
            summary += '\n\nحصيلة التصويت (بدون كشف أصوات الأشخاص):\n' + '\n'.join(
                f'{mention(self.player(game, int(uid)))}: {count}'
                for uid, count in game['vote_counts'].items())
        if result != 'cancelled':
            summary += '\n\nنقاط الجولة:\n' + '\n'.join(
                f'{mention(p)}: +{gains[p["uid"]]}' for p in game['players'])
        self.public(game, summary + '\n\n/game للكيم الجديد — /scores للترتيب')

    def tick(self):
        for game in self.s.active_games():
            if self.now() < game['deadline']:
                continue
            if game['stage'] in ('lobby', 'ready'):
                self.finish(game, 'cancelled', 'انتهى وقت الانضمام أو الجاهزية. الكيم انلغى بدون نقاط.')
            elif game['stage'] == 'questions':
                self.advance(game, timed_out=True)
            elif game['stage'] == 'voting':
                self.close_vote(game)
            elif game['stage'] == 'guess':
                self.finish(game, 'caught_wrong', 'انتهى وقت التخمين الأخير.')

    def handle(self, update):
        if 'callback_query' in update:
            self.callback(update['callback_query'])
        elif 'message' in update:
            self.message(update['message'])
        # Edits, channels, membership/service updates never count as turns or guesses.

    def message(self, msg):
        user = msg.get('from')
        text = msg.get('text', '').strip()
        if not user or user.get('is_bot') or not text:
            return
        uid, chat = user['id'], msg['chat']
        pieces = text.split(maxsplit=1)
        rawcmd = pieces[0] if text.startswith('/') else ''
        if '@' in rawcmd and rawcmd.split('@', 1)[1].lower() != self.username.lower():
            return
        command = rawcmd.split('@')[0].lower()
        arg = pieces[1].strip() if len(pieces) > 1 else ''
        if chat['type'] == 'private':
            if command == '/start':
                if arg.startswith('j_'):
                    self.join(arg[2:], user)
                else:
                    self.dm(uid, HELP)
                return
            self.tick()
            games = [g for g in self.s.active_games() if self.player(g, uid)]
            game = games[0] if games else None
            if command == '/leave':
                if game and game['stage'] == 'lobby':
                    game['players'] = [p for p in game['players'] if p['uid'] != uid]
                    self.s.save(game)
                    self.lobby(game)
                    self.dm(uid, 'طلعت من اللوبي.')
                else:
                    self.dm(uid, 'الخروج متاح قبل توزيع الأدوار فقط.')
            elif game and (command == '/guess' or (not command and game['stage'] == 'guess')):
                if arg or not command:
                    self.guess(game, uid, arg if command else text, True)
                else:
                    self.dm(uid, 'اكتب /guess وبعده الكلمة.')
            elif game and not command and game['stage'] == 'questions' and uid == game['outsider'] and self.bank.matches(self.bank.by_word[game['word']], text):
                self.guess(game, uid, text)
            elif command in ('/help', '/rules'):
                self.dm(uid, HELP)
            return
        if chat['type'] != 'supergroup':
            return
        topic = msg.get('message_thread_id', 0)
        context = {'chat': chat['id'], 'topic': topic}
        if command == '/bind':
            # Never send a response in General, even on an invalid bind request.
            if not chat.get('is_forum') or topic in (0, 1):
                return
            if not self.authorize(chat['id'], uid, True):
                self.public(context, 'ربط القسم للأدمن فقط.')
            elif self.s.active(chat['id']):
                self.public(context, 'كمّلوا أو ألغوا الكيم قبل تغيير القسم.')
            else:
                self.s.db.execute('INSERT OR REPLACE INTO bindings VALUES(?,?)', (chat['id'], topic))
                self.public(context, '✅ ربطت اللعبة بهذا القسم. رسائل الكيم تبقى هنا. /game للبدء.')
            return
        if self.s.binding(chat['id']) != topic or topic in (0, 1):
            return
        game = self.s.active(chat['id'], topic)
        if game and self.now() >= game['deadline']:
            self.tick()
            game = self.s.active(chat['id'], topic)
        if command == '/game':
            self.new_game(msg)
        elif command in ('/help', '/rules'):
            self.public(context, HELP)
        elif command == '/scores':
            scores = self.s.scoreboard(chat['id'])
            self.public(context, '🏆 نقاط الكروب\n' + ('\n'.join(
                f'{i}. {mention(p)} — {p["points"]}' for i, p in enumerate(scores, 1)) or 'بعد ماكو نقاط.'))
        elif command == '/status':
            if not game:
                self.public(context, 'ماكو كيم مفتوح. /game')
            elif game['stage'] == 'lobby':
                self.lobby(game)
            else:
                stages = {'ready': 'ننتظر الجاهزية', 'questions': 'الأسئلة', 'voting': 'التصويت الخاص', 'guess': 'التخمين الأخير'}
                self.public(game, f'الحالة: {stages[game["stage"]]}\nالباقي: {max(0, int(game["deadline"] - self.now()))} ثانية.')
        elif command == '/settings':
            if not arg:
                self.show_settings(context)
                return
            if not self.authorize(chat['id'], uid, True):
                self.public(context, 'تعديل إعدادات الكروب للأدمن فقط، حتى المضيف يحتاج يكون أدمن.')
                return
            try:
                seconds, count = map(int, arg.split())
                self.s.set_settings(chat['id'], seconds, count)
            except ValueError:
                self.public(context, 'مثال: /settings 35 12\nالوقت ٢٠–٩٠ ثانية؛ عدد الأسئلة زوجي من ٤ إلى ٤٠.')
                return
            self.show_settings(context, changed=True)
        elif command == '/cancel' and game:
            if uid == game['host'] or self.authorize(chat['id'], uid, True):
                self.finish(game, 'cancelled', 'المضيف أو الأدمن ألغى الكيم بدون نقاط.')
        elif command == '/resetwords':
            if not self.authorize(chat['id'], uid, True):
                return
            if game:
                self.public(game, 'إعادة البنك متاحة من ينتهي الكيم.')
            elif arg not in self.bank.by_category:
                self.public(context, 'اكتب كود التصنيف بعد الأمر:\n' + '\n'.join(
                    f'{c["id"]} — {html.escape(c["name"])}' for c in self.bank.categories))
            elif self.bank.remaining(arg, self.s.used(chat['id'])):
                self.public(context, 'بعد أكو كلمات ما لعبتوها بهذا التصنيف؛ ما نعيده قبل ما يخلص.')
            else:
                self.public(context, 'خلص التصنيف. تأكد تريد تعيد كلماته؟',
                            keys([[button('إي، أعيد هذا التصنيف', f'reset:{chat["id"]}:{topic}:{arg}:{uid}')]]))
        elif game and game['stage'] == 'questions' and self.player(game, uid):
            word = self.bank.by_word[game['word']]
            if uid != game['outsider'] and self.bank.contains(word, text):
                self.finish(game, 'cancelled', 'واحد من اللي داخل كشف السالفة. انلغت الجولة بدون نقاط.')
                self.s.enqueue('deleteMessage', {'chat_id': chat['id'], 'message_id': msg['message_id']})
                return
            if command == '/guess':
                if arg:
                    self.guess(game, uid, arg, True)
                return
            if command:
                return
            if self.bank.contains(word, text):
                if uid == game['outsider']:
                    self.guess(game, uid, word['text'])
                return
            a, b = game['turns'][game['turn']]
            if game['waiting'] == 'question' and uid == a:
                game['waiting'] = 'answer'
                self.s.save(game)
                self.public(game, f'{mention(self.player(game, b))} جاوب سؤال {mention(self.player(game, a))}.\n'
                            f'باقي {max(0, int(game["deadline"] - self.now()))} ثانية.')
            elif game['waiting'] == 'answer' and uid == b:
                self.advance(game)

    def callback(self, cb):
        uid = cb['from']['id']
        msg = cb.get('message', {})
        chat = msg.get('chat', {})
        parts = cb.get('data', '').split(':')
        if not parts:
            self.ack(cb, 'زر غير صالح')
            return
        op = parts[0]
        if op == 'cfg' and len(parts) == 5:
            try:
                group, topic, field, delta = int(parts[1]), int(parts[2]), parts[3], int(parts[4])
            except ValueError:
                self.ack(cb, 'زر غير صالح')
                return
            if (chat.get('id') != group or msg.get('message_thread_id') != topic
                    or self.s.binding(group) != topic or topic in (0, 1)
                    or not self.authorize(group, uid, True)
                    or (field, delta) not in {('seconds', -5), ('seconds', 5),
                                             ('question_count', -2), ('question_count', 2)}):
                self.ack(cb, 'التعديل للأدمن داخل قسم اللعبة فقط', True)
                return
            cfg = self.s.settings(group)
            cfg[field] += delta
            try:
                self.s.set_settings(group, cfg['seconds'], cfg['question_count'])
            except ValueError:
                self.ack(cb, 'وصلت الحد: الوقت ٢٠–٩٠ ثانية، الأسئلة ٤–٤٠', True)
                return
            self.show_settings({'chat': group, 'topic': topic}, changed=True)
            self.ack(cb, 'انحفظت، تطبّق على الكيم الجديد ✅')
            return
        if op == 'reset' and len(parts) == 5:
            try:
                group, topic, category, owner = int(parts[1]), int(parts[2]), parts[3], int(parts[4])
            except ValueError:
                self.ack(cb, 'زر غير صالح')
                return
            if (chat.get('id') != group or msg.get('message_thread_id') != topic or uid != owner
                    or self.s.binding(group) != topic or not self.authorize(group, uid, True)
                    or self.s.active(group) or category not in self.bank.by_category
                    or self.bank.remaining(category, self.s.used(group))):
                self.ack(cb, 'هذا التأكيد مو متاح هسه', True)
                return
            for w in self.bank.by_category[category]['words']:
                self.s.db.execute('DELETE FROM used WHERE chat=? AND word=?', (group, w['id']))
            self.public({'chat': group, 'topic': topic}, 'تمت إعادة التصنيف بموافقة الأدمن.')
            self.ack(cb)
            return
        game = self.s.game(parts[1]) if len(parts) > 1 else None
        if not game or game['stage'] == 'ended':
            self.ack(cb, 'هذا الكيم انتهى', True)
            return
        if self.now() >= game['deadline']:
            self.tick()
            self.ack(cb, 'انتهى وقت هذه المرحلة', True)
            return
        private = chat.get('type') == 'private' and chat.get('id') == uid
        public = (chat.get('id') == game['chat'] and msg.get('message_thread_id') == game['topic']
                  and self.s.binding(game['chat']) == game['topic'])
        if op in ('cats', 'cat', 'cancel'):
            if not public or uid != game['host']:
                self.ack(cb, 'هذا الزر للمضيف داخل قسم اللعبة', True)
                return
            if op == 'cancel':
                self.finish(game, 'cancelled', 'المضيف ألغى الكيم بدون نقاط.')
            elif game['stage'] != 'lobby':
                self.ack(cb, 'الكيم بدأ', True)
                return
            elif len(game['players']) < 3:
                self.ack(cb, 'نحتاج ٣ لاعبين على الأقل', True)
                return
            elif op == 'cats':
                self.categories(game)
            elif len(parts) == 3:
                self.begin(game, parts[2])
        elif op == 'ready':
            if not private or game['stage'] != 'ready' or not self.player(game, uid):
                self.ack(cb, 'هذا الدور مو إلك أو المرحلة انتهت', True)
                return
            if uid not in game['ready']:
                game['ready'].append(uid)
                self.s.save(game)
            if len(game['ready']) == len(game['players']):
                self.prompt(game)
        elif op == 'vote':
            try:
                target = int(parts[2])
            except (ValueError, IndexError):
                self.ack(cb, 'صوت غير صالح')
                return
            if (not private or game['stage'] != 'voting' or not self.player(game, uid)
                    or not self.player(game, target) or uid == target):
                self.ack(cb, 'التصويت غير متاح', True)
                return
            game['votes'][str(uid)] = target
            self.s.save(game)
            self.ack(cb, 'ثبتّت صوتك ✅ تگدر تغيّره قبل نهاية الدقيقة.')
            return
        else:
            self.ack(cb, 'زر غير صالح')
            return
        self.ack(cb)
