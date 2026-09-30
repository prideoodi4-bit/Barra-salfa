"""Arabic normalization and a canonical, alias-aware word bank."""
import hashlib
import json
import random
import re
import unicodedata
from pathlib import Path


def normalize(text):
    text = unicodedata.normalize('NFKC', text).lower()
    text = re.sub(r'[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06edـ]', '', text)
    text = text.translate(str.maketrans('أإآٱىةگچپڤک', 'اااايهكجبفك'))
    return ' '.join(re.sub(r'[^\w\s]', ' ', text, flags=re.UNICODE).split())


ALIASES = {
    'گص': ['قص', 'قص عراقي'], 'رگي': ['رقي', 'بطيخ أحمر', 'بطيخ احمر'],
    'بطيخ': ['شمام'], 'مسگوف': ['مسقوف', 'سمك مسكوف', 'سمچ مسگوف'],
    'دولمة': ['دولما'], 'باچة': ['باجة', 'باجه', 'پاچه'],
    'تمن وقيمة': ['تمن وقيمه', 'قيمة نجفية'], 'مرگ بامية': ['مرق بامية'],
    'مرگ سبانغ': ['مرق سبانخ'], 'سبانغ': ['سبانخ'], 'تكة': ['تكا'],
    'سمچ مطبگ': ['سمك مطبق'], 'قيمر': ['قشدة عراقية'], 'كليچة': ['كليجة'],
    'حلاوة دهين': ['دهين'], 'طماطة': ['طماطم', 'بندورة'], 'رمان': ['رمّان'],
    'نارنگي': ['يوسفي', 'مندلينا', 'نارنجى'], 'شلغم': ['لفت'],
    'قرنابيط': ['قرنبيط', 'زهرة'], 'لهانة': ['ملفوف'], 'چلب': ['كلب'],
    'قنفة': ['كنبة', 'أريكة'], 'ميز': ['طاولة'], 'زولية': ['سجادة'],
    'سبلت': ['سبليت', 'مكيف سبليت'], 'مبردة': ['مبردة هواء'],
    'مجمدة': ['فريزر'], 'دولاب': ['خزانة'], 'جزامة': ['خزانة أحذية'],
    'تراچي': ['تراجي', 'أقراط', 'حلق'], 'يشماغ': ['شماغ'],
    'جرابات': ['جوارب'], 'جنطة ظهر': ['حقيبة ظهر'], 'جنطة المدرسة': ['حقيبة المدرسة'],
    'تكسي': ['تاكسي', 'سيارة أجرة'], 'باص': ['حافلة'], 'تك تك': ['تكتك', 'توك توك'],
    'درّاجة هوائية': ['بسكليت'], 'دراجة هوائية': ['بسكليت', 'بايسكل'],
    'طيارة': ['طائرة'], 'گراج': ['كراج'], 'محطة بانزين': ['محطة بنزين', 'محطة وقود'],
    'بيتزا': ['pizza'], 'برگر': ['برجر', 'برغر', 'burger'],
    'آيس كريم': ['ايسكريم', 'بوظة', 'مثلجات'], 'تشيز كيك': ['cheesecake'],
    'چيز كيك': ['تشيز كيك', 'cheesecake'], 'سباگيتي': ['سباغيتي', 'سباغتي'],
    'كرة قدم': ['كرة القدم', 'football', 'soccer'], 'كرة سلة': ['كرة السلة', 'basketball'],
    'ببجي': ['pubg'], 'فري فاير': ['free fire'], 'فورتنايت': ['fortnite'],
    'كول أوف ديوتي': ['call of duty', 'كول اوف دوتي'],
    'كاونتر سترايك': ['counter strike', 'cs', 'cs2'], 'فالورانت': ['valorant'],
    'قراند': ['grand theft auto', 'gta', 'جراند'], 'ماينكرافت': ['minecraft'],
    'روبلوكس': ['roblox'], 'فيفا': ['fifa', 'ea fc'], 'بيس': ['pes', 'efootball'],
    'گود أوف وور': ['god of war', 'جود اوف وار'], 'أمانگ أس': ['among us', 'امونج اس'],
    'تلكرام': ['تلغرام', 'تيليجرام', 'تليجرام', 'telegram'],
    'واتساب': ['واتس اب', 'whatsapp'], 'إنستغرام': ['انستكرام', 'instagram'],
    'فيسبوك': ['فيس بوك', 'facebook'], 'إكس': ['تويتر', 'twitter', 'x'],
    'سنابچات': ['سناب شات', 'سنابجات', 'snapchat'], 'يوتيوب': ['youtube'],
    'گوگل مابس': ['خرائط جوجل', 'google maps'], 'چات جي بي تي': ['شات جي بي تي', 'chatgpt'],
    'نتفلكس': ['نتفليكس', 'netflix'], 'كاب كت': ['capcut'],
    'شكرلمة': ['شكرلمه'], 'زنود الست': ['زنود ست'], 'داطلي': ['داطلي عراقي'],
    'لبن': ['لبن زبادي', 'زبادي'], 'باگلاء': ['باقلاء', 'فول أخضر'],
    'بازلاء': ['بزاليا', 'بسلة'], 'كفوف': ['قفازات'],
    'حصالة': ['حصالة فلوس'], 'سبورة': ['سبورة مدرسية'],
    'لعبة الثعبان': ['لعبة الحية', 'لعبة الافعى', 'snake'],
    'فروزن': ['frozen', 'ملكة الثلج'], 'الأسد الملك': ['الاسد الملك', 'lion king', 'the lion king'],
    'شريك': ['shrek'], 'توي ستوري': ['toy story', 'حكاية لعبة'],
    'سندريلا': ['cinderella'],
    'أربيل': ['اربيل', 'هولير'], 'الناصرية': ['ناصرية'], 'العمارة': ['عمارة'],
    'بريطانيا': ['المملكة المتحدة', 'انجلترا'], 'أمريكا': ['الولايات المتحدة', 'امريكا', 'usa'],
    'الإمارات': ['الامارات العربية المتحدة'], 'زردة': ['زرده'],
}


class Bank:
    def __init__(self, path=None):
        path = Path(path or Path(__file__).with_name('words.json'))
        self.categories = json.loads(path.read_text(encoding='utf-8'))
        self.by_category = {c['id']: c for c in self.categories}
        self.by_word = {w['id']: w for c in self.categories for w in c['words']}
        seen = {}
        for c in self.categories:
            for w in c['words']:
                for a in w['aliases']:
                    a = normalize(a)
                    if a in seen and seen[a] != w['id']:
                        raise ValueError(f'Conflicting alias: {a}')
                    seen[a] = w['id']

    def remaining(self, category, used):
        return [w for w in self.by_category[category]['words'] if w['id'] not in used]

    def choose(self, category, used, rng=None):
        choices = self.remaining(category, used)
        return (rng or random.SystemRandom()).choice(choices) if choices else None

    def matches(self, word, text):
        return normalize(text) in {normalize(a) for a in word['aliases']}

    def contains(self, word, text):
        value = ' ' + normalize(text) + ' '
        # Multiword and token boundaries prevent قط from matching قطار.
        return any(' ' + normalize(a) + ' ' in value for a in word['aliases'])


def build():
    base = Path(__file__).parent
    categories = []
    for n, line in enumerate(base.joinpath('words_source.txt').read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        name, values = line.split('|', 1)
        words = []
        for value in values.split('،'):
            value = value.strip()
            canonical = normalize(value)
            word_id = hashlib.sha256(canonical.encode()).hexdigest()[:16]
            aliases = list(dict.fromkeys([value, *ALIASES.get(value, [])]))
            words.append({'id': word_id, 'text': value, 'aliases': aliases})
        categories.append({'id': f'c{n:02}', 'name': name, 'words': words})
    base.joinpath('words.json').write_text(json.dumps(categories, ensure_ascii=False, indent=2), encoding='utf-8')
    bank = Bank()
    print(f'{len(bank.categories)} categories; {len(bank.by_word)} canonical words')


if __name__ == '__main__':
    build()
