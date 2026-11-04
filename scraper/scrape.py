"""Reads each blog in sources.json, finds recipes, writes data/recipes.json.
Only structured facts are stored (title, ingredients, tags, link) - the method
stays on the original blog. Safe to re-run: unchanged posts are skipped."""
import json, re, time, pathlib, os
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA, SEEN = ROOT / "data" / "recipes.json", ROOT / "data" / "seen.json"
UA = {"User-Agent": "Mozilla/5.0 (compatible; PantryRecipesBot/1.0; personal recipe finder)", "Accept": "text/html,application/xml,application/json;q=0.9,*/*;q=0.8"}
LOG = []
def log(m): print(m); LOG.append(m)
SCHEMA = 5                                      # bump to re-fetch everything
MAX_NEW = int(os.environ.get("MAX_NEW", 1500))   # posts fetched per run
DELAY = 2.0                                     # seconds between page requests

TAGS = {  # tag -> keywords found in title/categories/description
 "Salad": ["salad", "slaw"], "Soup & Stew": ["soup", "stew", "chili", "chowder", "dal "],
 "Dessert & Treats": ["dessert", "brownie", "cookie", "cake", "treat", "pudding", "muffin", "sweet"],
 "Breakfast": ["breakfast", "pancake", "oatmeal", "porridge", "granola"],
 "Dinner": ["dinner", "main course"], "Dips & Sauces": ["dip", "hummus", "sauce", "dressing", "spread", "condiment"],
 "Burgers & Patties": ["burger", "patty", "patties", "falafel", "fritter"],
 "Pasta": ["pasta", "orzo", "noodle", "spaghetti"], "Curry": ["curry"], "Bowls": ["bowl"],
 "Sandwiches & Wraps": ["sandwich", "wrap", "toast", "tostada", "arayes"],
 "Sides": ["side dish", "roasted", "side"], "Bread": ["bread", "bun"],
 "Drinks": ["smoothie", "coffee", "beverage", "drink", "latte", "water recipe"],
 "High-Protein": ["high-protein", "protein"], "Oil-Free": ["oil-free", "without oil", "no oil"],
 "Gluten-Free": ["gluten-free", "gluten free"],
}
INGR_TAGS = {"Tofu": ["tofu"], "Tempeh": ["tempeh"], "Lentils": ["lentil"], "Chickpeas": ["chickpea", "garbanzo"],
             "Beans": ["beans", "bean "], "Quinoa": ["quinoa"], "Cabbage": ["cabbage"]}
UNITS = r"(cups?|tablespoons?|tbsp|teaspoons?|tsp|ounces?|oz|pounds?|lbs?|lb|grams?|g|kg|ml|l|cans?|pinch|dash|large|medium|small|handful|bunch|slices?|packages?|sticks?|inch)"
ADJ = r"^(?:(?:a|an|the|variety|of|fresh|dried|grated|chopped|sliced|diced|frozen|more)\s+)+"

def get(url):
    last = None
    for wait in (0, 5, 20, 60):          # retry politely if the site is busy or throttling
        if wait: time.sleep(wait)
        try: r = requests.get(url, headers=UA, timeout=30)
        except requests.RequestException as e: last = e; continue
        if r.status_code in (429, 403, 500, 502, 503, 504): last = Exception(f"HTTP {r.status_code}"); continue
        r.raise_for_status(); return r.text
    raise last

def sitemap_urls(url, depth=0):
    out = {}
    xml = get(url)
    for blk in re.findall(r"<(?:url|sitemap)>.*?</(?:url|sitemap)>", xml, re.S):
        loc = re.search(r"<loc>(.*?)</loc>", blk).group(1).strip()
        mod = (re.search(r"<lastmod>(.*?)</lastmod>", blk) or [None, ""])[1]
        if loc.endswith(".xml"):
            if depth < 2 and not re.search(r"page|categor|tag|author|attachment|product|web-stor", loc):
                out.update(sitemap_urls(loc, depth + 1))
        else:
            out[loc] = mod
    return out

def find_recipe(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try: stack = [json.loads(s.string or "")]
        except Exception: continue
        while stack:
            x = stack.pop()
            if isinstance(x, list): stack += x
            elif isinstance(x, dict):
                t = x.get("@type"); t = t if isinstance(t, list) else [t]
                if "Recipe" in t: return x
                stack += list(x.get("@graph", []))
    return None

FR = {"¼": .25, "½": .5, "¾": .75, "⅓": 1/3, "⅔": 2/3, "⅛": .125, "⅜": .375}
UMAP = {"cup": "cup", "cups": "cup", "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp", "teaspoon": "tsp",
        "teaspoons": "tsp", "tsp": "tsp", "ounce": "oz", "ounces": "oz", "oz": "oz", "pound": "lb", "pounds": "lb",
        "lb": "lb", "lbs": "lb", "gram": "g", "grams": "g", "g": "g", "kg": "kg", "ml": "ml", "l": "l", "can": "can",
        "cans": "can", "tin": "can", "tins": "can", "bunch": "bunch", "bunches": "bunch", "handful": "handful",
        "pinch": "pinch", "dash": "dash", "slice": "slice", "slices": "slice", "stalk": "stalk", "stalks": "stalk",
        "sprig": "sprig", "sprigs": "sprig", "head": "head", "heads": "head", "block": "block", "blocks": "block",
        "package": "package", "packages": "package", "inch": "inch", "clove": "clove", "cloves": "clove"}

def tonum(t):
    if t in FR: return FR[t]
    m = re.fullmatch(r"(\d+)([¼½¾⅓⅔⅛⅜])", t)
    if m: return int(m.group(1)) + FR[m.group(2)]
    if re.fullmatch(r"\d+/\d+", t): a, b = t.split("/"); return int(a) / int(b)
    if re.fullmatch(r"\d+(\.\d+)?", t): return float(t)

NUMW = [(r"^(a |one )?quarter\b", "¼"), (r"^three quarters?\b", "¾"), (r"^(a |one )?half\b", "½"), (r"^(a |one )?third\b", "⅓"), (r"^one\b", "1"), (r"^two\b", "2"),
        (r"^three\b", "3"), (r"^four\b", "4"), (r"^five\b", "5"), (r"^six\b", "6"), (r"^(a|an)\b(?= (pinch|dash|handful|bunch|splash|can|clove|small|large|medium))", "1")]
def numwords(t):
    t = t.strip().lower()
    for pat, rep in NUMW:
        if re.match(pat, t): return re.sub(pat, rep, t, count=1)
    return t

def parse_qty(text):
    """'1 ½ cups dried red lentils' -> (1.5, 'cup'). Ranges use the upper number."""
    raw = re.sub(r"\(.*?\)", "", numwords(text).replace("▢", "")).replace("–", "-")
    toks, q, i = raw.split(), None, 0
    while i < len(toks):
        t = toks[i]; r = re.fullmatch(r"(\d+)-(\d+)", t)
        if r: q = float(r.group(2)); i += 1; continue
        if t in ("to", "-") and q is not None and i + 1 < len(toks) and tonum(toks[i + 1]) is not None:
            q = tonum(toks[i + 1]); i += 2; continue
        n = tonum(t)
        if n is None: break
        q = (q or 0) + n; i += 1
    while i < len(toks) and toks[i] in ("large", "medium", "small", "heaping", "big"): i += 1
    u = UMAP.get(toks[i].strip(".,")) if i < len(toks) else None
    if not u:
        m = re.search(r"\b(cloves?|cans?|tins?|blocks?|packages?)\b", raw)
        if m: u = UMAP[m.group(1)]
    return q, u or ""

def clean_ingredient(text):
    orig = text; text = numwords(text)
    t = re.sub(r"\(.*?\)", "", text.lower().replace("▢", ""))
    optional = "optional" in text.lower()
    t = re.sub(r"\bor to taste\b|\boptional\b|\bto taste\b", "", t).split(",")[0]
    t = re.split(r"\babout\b", t)[0]
    t = re.sub(r"\b(with|plus|such as|like|for|divided|at room temperature|on hand|what you have|or more)\b.*", "", t).strip()
    names = []
    for part in re.split(r"\bor\b", t):
        toks = part.split()
        while toks and (re.fullmatch(r"[\d¼½¾⅓⅔⅛/.\-–]+", toks[0]) or re.fullmatch(UNITS, toks[0].strip(".")) or toks[0] == "of"):
            toks.pop(0)
        if toks: names.append(" ".join(toks))
    q, u = parse_qty(text)
    mc = re.search(r"\babout (\d+) (?:small|medium|large|whole|big)\b", text.lower())
    return {"text": orig.strip(), "names": names or [t.strip()], "opt": optional, "q": q, "u": u, "c": int(mc.group(1)) if mc else None}

def parse_subs(soup):
    h = next((h for h in soup.find_all(["h2", "h3"]) if "substitution" in h.get_text().lower()), None)
    out = []
    if not h: return out
    for el in h.find_next_siblings():
        if el.name in ("h2", "h3"): break
        for li in (el.find_all("li") if el.name in ("ul", "ol") else []):
            m = re.match(r"(.+?)\s[-–—:]\s*(.+)", li.get_text(" ", strip=True))
            m2 = m and re.search(r"\b(?:use|try|swap|substitute|replace with)\b\s+(.*)", m.group(2), re.I)
            if not m2: continue
            chunk = re.split(r"\b(?:using|in place of|as desired|the same)\b|[.;(]", m2.group(1))[0]
            alts = [re.sub(ADJ, "", a.strip().lower()) for a in re.split(r",|\bor\b|\band\b", chunk)]
            alts = [a for a in alts if a and not re.search(r"\d", a)]
            if alts: out.append({"for": m.group(1).strip().lower(), "use": alts})
    return out

def parse_prep(soup, rec):
    txt = []
    def walk(x):
        if isinstance(x, str): txt.append(x)
        elif isinstance(x, list): [walk(i) for i in x]
        elif isinstance(x, dict): walk(x.get("text") or x.get("itemListElement") or "")
    walk(rec.get("recipeInstructions") or [])
    t = " ".join(txt).lower()
    flags = [f for f, pat in (("soak", r"\bsoak"), ("marinate", r"marinat|press the tofu|press tofu"),
             ("chill", r"chill|overnight|refrigerate for|set in the fridge")) if re.search(pat, t)]
    h = next((h for h in soup.find_all(["h2", "h3"]) if "storage" in h.get_text().lower()), None)
    st = ""
    if h:
        for el in h.find_next_siblings():
            if el.name in ("h2", "h3"): break
            st += " " + el.get_text(" ", strip=True)
    st = st.lower()
    d = re.search(r"(\d+)\s*(?:-|to)?\s*\d*\s*days", st)
    return {"flags": flags, "days": int(d.group(1)) if d else None, "freeze": "freez" in st}

NKEYS = {"calories": "kcal", "carbohydrates": "carbs", "carbohydrate": "carbs", "protein": "protein", "fat": "fat",
         "saturated_fat": "satfat", "sodium": "sodium", "potassium": "potassium", "fiber": "fibre", "fibre": "fibre",
         "sugar": "sugar", "vitamin_a": "vitA", "vitamin_c": "vitC", "calcium": "calcium", "iron": "iron"}
NBASE = {"kcal": "kcal", "protein": "g", "carbs": "g", "fat": "g", "satfat": "g", "fibre": "g", "sugar": "g",
         "sodium": "mg", "potassium": "mg", "calcium": "mg", "iron": "mg", "vitC": "mg", "vitA": "ug"}
NDV = {"kcal": 2000, "protein": 50, "carbs": 275, "fat": 78, "satfat": 20, "fibre": 28, "sugar": 50, "sodium": 2300,
       "potassium": 4700, "calcium": 1300, "iron": 18, "vitC": 90, "vitA": 900}

def to_std(key, num, unit):
    """Convert a label value to: kcal, g, mg or (vitamin A) micrograms RAE."""
    u = (unit or "").lower().replace("\u03bc", "\u00b5").replace("mcg", "ug").replace("\u00b5g", "ug").strip(" .")
    if key == "kcal": return num
    if u == "%": return num * NDV[key] / 100
    if u == "iu": return num * 0.05 if key == "vitA" else None
    f = {"g": 1, "mg": 1e-3, "ug": 1e-6, "kg": 1e3}.get(u)
    if f is None: return num if not u else None
    return num * f * {"g": 1, "mg": 1e3, "ug": 1e6}[NBASE[key]]

def parse_nutrition(soup, rec):
    """Per-serving nutrition from the recipe card label, falling back to the page's structured data."""
    out = {}
    for el in soup.select('[class*="nutrition-container-"]'):
        m = re.search(r"nutrition-container-([a-z_-]+)", " ".join(el.get("class", [])))
        k = NKEYS.get(m.group(1).replace("-", "_")) if m else None
        v = el.select_one('[class*="nutrition-value"]'); u = el.select_one('[class*="nutrition-unit"]')
        num = re.sub(r"[^\d.]", "", v.get_text()) if v else ""
        if k and num:
            val = to_std(k, float(num), u.get_text() if u else "")
            if val is not None: out[k] = round(val, 1)
    n = rec.get("nutrition") or {}
    for jk, k in (("calories", "kcal"), ("proteinContent", "protein"), ("carbohydrateContent", "carbs"), ("fatContent", "fat"),
                  ("saturatedFatContent", "satfat"), ("fiberContent", "fibre"), ("sugarContent", "sugar"), ("sodiumContent", "sodium")):
        m = re.match(r"\s*([\d.]+)\s*([a-z\u00b5\u03bc%]*)", str(n.get(jk, "")).lower())
        if k not in out and m:
            val = to_std(k, float(m.group(1)), m.group(2))
            if val is not None: out[k] = round(val, 1)
    return out

def instr_text(rec):
    txt = []
    def walk(x):
        if isinstance(x, str): txt.append(x)
        elif isinstance(x, list): [walk(i) for i in x]
        elif isinstance(x, dict): walk(x.get("text") or x.get("itemListElement") or "")
    walk(rec.get("recipeInstructions") or [])
    return " ".join(txt).lower()

def parse_flags(rec, title, ingredients):
    """Does it need a blender? Is it served hot, cold or either?"""
    t = instr_text(rec); ti = title.lower()
    blender = bool(re.search(r"\bblend|food processor|puree|pur\u00e9e|vitamix|immersion|nutribullet|magic bullet", t + " " + " ".join(ingredients).lower()))
    heat = bool(re.search(r"\b(bake|roast|simmer|boil|saut[e\u00e9]|fry|sear|heat|preheat|grill|steam|cook)\b", t))
    cold_t = bool(re.search(r"salad|smoothie|pudding|dip|hummus|dressing|overnight|no-bake|no bake|slaw|spread|gazpacho|chilled|sauce", ti))
    hot_t = bool(re.search(r"soup|stew|curry|chili|burger|pancake|porridge|casserole|roast|baked|pasta|stir[- ]fry|ramen|noodle|skillet|bake|patties|fritter", ti))
    if hot_t and not cold_t: temp = "hot"
    elif cold_t and hot_t: temp = "either"
    elif cold_t: temp = "either" if heat else "cold"
    else: temp = "hot" if heat else "cold"
    return {"blender": blender, "temp": temp}

FOODS_SRC = """
red lentil/split lentil;358,24,60,1.1,11,7.5,35,677,2,4.5,6,0.2;192;;;cooked,canned
green lentil/brown lentil/french lentil/lentil;352,25,63,1.1,11,6.5,35,677,2,4.5,6,0.2;192;;;cooked,canned
lentil;116,9,20,0.4,8,3.3,19,369,0,1.5,2,0.1;198;;cooked,canned;
chickpea/garbanzo;164,8.9,27,2.6,7.6,2.9,49,291,1,1.3,7,0.3;164;240;;dried,dry
chickpea/garbanzo;378,20,63,6,12,4.3,57,718,3,4,24,0.6;200;;dried,dry;
black bean;132,8.9,24,0.5,8.7,2.1,27,355,0,0,1,0.1;172;240;;dried,dry
black bean;341,21.6,62,1.4,15.5,5,123,1483,0,0,5,0.4;194;;dried,dry;
kidney bean;127,8.7,23,0.5,6.4,2.2,28,403,0,1,1,0.1;177;240;;dried,dry
white bean/cannellini/navy bean/great northern/butter bean/gigante;140,9.7,25,0.6,6.3,2.5,90,561,0,0,6,0.1;179;240;;dried,dry
pinto bean/refried bean;143,9,26,0.7,9,2.1,46,436,0,1,1,0.1;171;240;;dried,dry
bean;130,8.5,23,0.6,7,2.1,40,400,0,0.5,3,0.1;175;240;;dried,dry
mung bean;105,7,19,0.4,7.6,1.4,27,266,2,1,2,0.1;202;;;
edamame;121,12,9,5.2,5.2,2.3,63,436,9,6,6,0.6;155;;;
pea;81,5.4,14.5,0.4,5.7,1.5,25,244,38,40,5,0.1;145;;;split
tofu;144,17,3,9,2,2.7,350,237,0,0,14,1.3;252;396;;
tempeh;192,20,7.6,11,0,2.7,111,412,0,0,9,2.2;166;;;
seitan;370,75,14,1.9,0.6,5.2,142,100,0,0,29,0.3;;;;
rice;365,7,80,0.7,1.3,0.8,28,115,0,0,5,0.2;185;;;cooked
brown rice;367,7.5,76,3.2,3.6,1.5,33,223,0,0,4,0.6;190;;;cooked
rice;130,2.7,28,0.3,0.4,0.2,10,35,0,0,1,0.1;158;;cooked;
quinoa;368,14,64,6,7,4.6,47,563,0,0,5,0.7;170;;;cooked
quinoa;120,4.4,21,1.9,2.8,1.5,17,172,0,0,7,0.2;185;;cooked;
oat/oatmeal;379,13,68,6.5,10,4.3,52,362,0,0,6,1.1;90;;;
pasta/spaghetti/penne/orzo/noodle/macaroni;371,13,75,1.5,3.2,3.3,21,223,0,0,6,0.3;100;;;
barley;352,9.9,78,1.2,15.6,2.5,29,280,0,0,9,0.2;200;;;
whole wheat flour;340,13,72,2.5,11,3.9,34,363,0,0,2,0.4;120;;;
chickpea flour/besan;387,22,58,6.7,11,4.9,45,846,3,0,64,0.7;92;;;
flour;364,10,76,1,2.7,4.6,15,107,0,0,2,0.2;125;;;
bread/pita/tortilla/wrap;265,9,49,3.2,7,3.6,260,115,0,0,491,0.7;;40;;
almond;579,21,22,50,12.5,3.7,269,733,0,0,1,3.8;143;;;milk,butter,flour
cashew;553,18,30,44,3.3,6.7,37,660,0,0.5,12,7.8;137;;;milk,butter
walnut;654,15,14,65,6.7,2.9,98,441,1,1.3,2,6.1;117;;;
peanut;567,26,16,49,8.5,4.6,92,705,0,0,18,6.8;146;;;butter
pumpkin seed/pepita;559,30,11,49,6,8.8,46,809,1,1.9,7,8.7;129;;;
sunflower seed;584,21,20,51,8.6,5.3,78,645,3,1.4,9,4.5;140;;;
sesame seed/sesame;573,18,23,50,12,14.6,975,468,0,0,11,7;144;;;oil
chia;486,17,42,31,34,7.7,631,407,0,1.6,16,3.3;170;;;
flax/flaxseed;534,18,29,42,27,5.7,255,813,0,0.6,30,3.7;168;;;
hemp;553,32,9,49,4,7.9,70,1200,1,0.5,5,4.5;160;;;
tahini;595,17,21,54,9.3,8.9,426,414,1,0,115,7.5;240;;;
peanut butter/almond butter/cashew butter/seed butter;600,23,20,52,7,2.5,100,700,0,0,17,8;256;;;
onion;40,1.1,9.3,0.1,1.7,0.2,23,146,0,7,4,0;160;110;;powder
garlic;149,6.4,33,0.5,2.1,1.7,181,401,0,31,17,0.1;136;3;;powder
garlic powder;331,16,73,0.7,9,5.8,79,1193,0,1,60,0.1;144;;;
ginger;80,1.8,18,0.8,2,0.6,16,415,0,5,13,0.2;96;6;;ground,powder
ground ginger;335,9,72,4.2,14,20,114,1320,2,0.7,27,1;96;;;
carrot;41,0.9,9.6,0.2,2.8,0.3,33,320,835,5.9,69,0;128;61;;
sweet potato;86,1.6,20,0.1,3,0.6,30,337,709,2.4,55,0;133;130;;
potato;77,2,17,0.1,2.2,0.8,12,421,1,19.7,6,0;150;170;;
tomato paste;82,4.3,19,0.5,4.1,3,36,1014,76,21,59,0.1;262;16;;
crushed tomato/diced tomato/tomato sauce/passata;32,1.6,7,0.3,1.5,0.8,27,297,15,9,132,0;245;400;;
tomato;18,0.9,3.9,0.2,1.2,0.3,10,237,42,13.7,5,0;180;123;;
spinach;23,2.9,3.6,0.4,2.2,2.7,99,558,469,28,79,0.1;30;;;
kale;49,4.3,8.8,0.9,3.6,1.5,150,491,241,120,38,0.1;67;;;
cabbage;25,1.3,5.8,0.1,2.5,0.5,40,170,5,36,18,0;89;;;
broccoli;34,2.8,6.6,0.4,2.6,0.7,47,316,31,89,33,0.1;91;;;
cauliflower;25,1.9,5,0.3,2,0.4,22,299,0,48,30,0.1;107;;;
zucchini;17,1.2,3.1,0.3,1,0.4,16,261,10,17,8,0.1;124;196;;
cucumber;15,0.7,3.6,0.1,0.5,0.3,16,147,5,2.8,2,0;104;300;;
bell pepper;31,1,6,0.3,2.1,0.4,7,211,157,128,4,0;149;119;;
mushroom;22,3.1,3.3,0.3,1,0.5,3,318,0,2,5,0;70;;;
celery;16,0.7,3,0.2,1.6,0.2,40,260,22,3,80,0;101;40;;
beetroot/beet;43,1.6,9.6,0.2,2.8,0.8,16,325,2,4.9,78,0;136;82;;
corn;86,3.3,19,1.4,2.7,0.5,2,270,9,6.8,15,0.2;145;;;
asparagus;20,2.2,3.9,0.1,2.1,2.1,24,202,38,5.6,2,0;134;;;
cilantro/coriander;23,2.1,3.7,0.5,2.8,1.8,67,521,337,27,46,0;16;;;
parsley;36,3,6.3,0.8,3.3,6.2,138,554,421,133,56,0.1;60;;;
basil;23,3.2,2.7,0.6,1.6,3.2,177,295,264,18,4,0;24;;;
green onion/scallion;32,1.8,7.3,0.2,2.6,1.5,72,276,50,18.8,16,0;100;15;;
leek;61,1.5,14,0.3,1.8,2.1,59,180,83,12,20,0;89;89;;
shallot;72,2.5,17,0.1,3.2,1.2,37,334,0,8,12,0;160;40;;
avocado;160,2,8.5,14.7,6.7,0.6,12,485,7,10,7,2.1;150;150;;
lemon juice/lime juice;22,0.4,6.9,0.2,0.3,0.1,6,103,1,39,1,0;240;;;
lemon;29,1.1,9.3,0.3,2.8,0.6,26,138,1,53,2,0;;60;;
lime;30,0.7,11,0.2,2.8,0.6,33,102,2,29,2,0;;44;;
banana;89,1.1,23,0.3,2.6,0.3,5,358,3,8.7,1,0.1;150;118;;
apple;52,0.3,14,0.2,2.4,0.1,6,107,3,4.6,1,0;125;182;;
blueberry;57,0.7,14,0.3,2.4,0.3,6,77,3,9.7,1,0;148;;;
strawberry;32,0.7,7.7,0.3,2,0.4,16,153,1,59,1,0;152;;;
mango;60,0.8,15,0.4,1.6,0.2,11,168,54,36,1,0.1;165;;;
date;282,2.5,75,0.4,8,1,39,696,0,0,2,0;147;24;;
raisin;299,3.1,79,0.5,3.7,1.9,50,749,0,2.3,11,0.1;145;;;
coconut milk/coconut cream;197,2,2.8,21,0,3.3,18,263,0,2.8,13,19;240;400;;
coconut flake/shredded coconut;660,6.9,24,65,16,3.3,26,543,0,1.5,20,57;80;;;
oil/olive oil/avocado oil/vegetable oil/sesame oil;884,0,0,100,0,0.6,1,1,0,0,2,14;216;;;
maple syrup/agave/date syrup/honey;290,0,72,0.1,0,0.2,70,200,0,0,10,0;320;;;
sugar;387,0,100,0,0,0.1,1,2,0,0,1,0;200;;;
soy sauce/tamari/liquid aminos/coconut aminos;53,8,5,0,0.8,2.4,33,435,0,0,5493,0;255;;;
miso;199,12,26,6,5.4,2.5,57,210,0,0,3728,1;275;;;
mustard;66,4.4,5.8,4,4,1.6,63,138,0,0,1135,0.3;250;;;
vinegar;18,0,0,0,0,0.2,7,73,0,0,2,0;240;;;
vegetable broth/stock/broth;6,0.3,1,0.1,0,0.1,5,30,0,0,350,0;240;;;
nutritional yeast;357,50,36,5,20,5,50,800,0,0,50,0.7;60;;;
plant milk/almond milk/oat milk/soy milk/milk;30,1.5,3,1.2,0.3,0.3,120,70,50,0,50,0.2;240;;;
cocoa/cacao;228,20,58,14,33,14,128,1524,0,0,21,8;86;;;
dark chocolate/chocolate chip;546,5,60,31,7,12,56,559,0,0,20,18;170;;;
salt;0,0,0,0,0,0.3,24,8,0,0,38758,0;288;;;
black pepper/pepper;251,10,64,3.3,25,9.7,443,1329,27,0,20,1.4;108;;;
turmeric;312,9.7,67,3.3,22.7,55,183,2525,0,0.7,38,1.8;144;;;
cumin;375,18,44,22,10.5,66,931,1788,64,7.7,168,1.5;96;;;
paprika;282,14,54,13,35,21,229,2280,2463,0,68,2;112;;;
cinnamon;247,4,81,1.2,53,8.3,1002,431,15,3.8,10,0.3;125;;;
oregano/thyme/rosemary/dill/mint/sage;265,9,69,4.3,43,36,1597,1260,85,2.3,25,1.6;48;;;
chili powder/curry powder/garam masala/cayenne/italian seasoning/spice/seasoning;282,13,50,14,34,17,330,1950,1000,30,1000,2.5;128;;;
onion powder;341,10,79,1,15,3.9,384,985,0,0,73,0.2;115;;;
"""
NUTK = ["kcal", "protein", "carbs", "fat", "fibre", "iron", "calcium", "potassium", "vitA", "vitC", "sodium", "satfat"]

def _stem(w):
    w = re.sub(r"ies$", "y", w); w = re.sub(r"(o|ch|sh|x)es$", r"\\1", w); return re.sub(r"s$", "", w)
def _words(t): return [_stem(w) for w in re.sub(r"[^a-z\\s]", " ", t.lower()).split()]
FOODS = []
for _l in FOODS_SRC.strip().splitlines():
    k, n, cup, each, req, ex = _l.split(";")
    FOODS.append({"keys": [_words(x) for x in k.split("/")], "n": [float(x) for x in n.split(",")], "cup": float(cup) if cup else None,
                  "each": float(each) if each else None, "req": {_stem(x) for x in req.split(",") if x}, "ex": {_stem(x) for x in ex.split(",") if x}})

def match_food(text, unit):
    w = set(_words(text)); best = None
    if unit == "can": w.add("canned")
    for f in FOODS:
        if (f["req"] and not (f["req"] & w)) or (f["ex"] & w): continue
        for kw in f["keys"]:
            if kw and all(x in w for x in kw) and (not best or len(kw) > best[0]): best = (len(kw), f)
    return best[1] if best else None

def to_grams(f, q, u, c):
    cup = f["cup"] or 240
    if c: return c * (f["each"] or 100)
    if u == "g": return q
    if u == "kg": return q * 1000
    if u == "oz": return q * 28.35
    if u == "lb": return q * 453.6
    if u == "cup": return q * cup
    if u == "tbsp": return q * cup / 16
    if u == "tsp": return q * cup / 48
    if u == "ml": return q * cup / 240
    if u == "l": return q * cup * 1000 / 240
    if u == "pinch": return q * 0.4
    if u == "dash": return q * 0.6
    if u == "handful": return q * 25
    return q * f["each"] if f["each"] else None

def estimate_nutrition(ings, servings):
    """Rough per-serving nutrition from the ingredient list and a built-in food table (USDA-style values)."""
    tot, used, total = [0.0] * 12, 0, 0
    for i in ings:
        if i["opt"] or i["q"] is None and not i["c"]: continue
        total += 1
        f = match_food(i["text"], i["u"])
        g = to_grams(f, i["q"] or 1, i["u"], i["c"]) if f else None
        if not g: continue
        used += 1
        for k in range(12): tot[k] += f["n"][k] * g / 100
    if not used: return {}, 0
    per = {k: round(v / max(servings, 1), 1) for k, v in zip(NUTK, tot)}
    per["kcal"] = int(round(per["kcal"]))
    return per, round(used / total, 2) if total else 0

def steps_list(rec):
    out = []
    def walk(x):
        if isinstance(x, str): out.append(x)
        elif isinstance(x, list): [walk(i) for i in x]
        elif isinstance(x, dict):
            if x.get("itemListElement"): walk(x["itemListElement"])
            elif x.get("text"): out.append(x["text"])
    walk(rec.get("recipeInstructions") or [])
    return [t for t in (re.sub(r"\\s+", " ", BeautifulSoup(x, "html.parser").get_text()).strip() for x in out) if t]

def servings_of(rec):
    y = rec.get("recipeYield"); y = " ".join(map(str, y)) if isinstance(y, list) else str(y or "")
    m = re.search(r"\\d+", y); return (int(m.group(0)) if m else 4), y.strip()

def tag_recipe(title, cats, desc, ings):
    hay = f" {title} {cats} {desc} ".lower()
    tags = [t for t, kws in TAGS.items() if any(k in hay for k in kws)]
    ihay = (title + " " + " ".join(i["text"] for i in ings)).lower()
    return tags + [t for t, kws in INGR_TAGS.items() if any(k in ihay for k in kws)]

def minutes(iso):
    m = re.match(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", iso or "")
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) or None if m else None

def short(d, n=150):
    d = re.sub(r"\s+", " ", BeautifulSoup(d or "", "html.parser").get_text()).strip()
    return d if len(d) <= n else d[:n].rsplit(" ", 1)[0] + "…"

def first(v):
    v = v[0] if isinstance(v, list) and v else v
    return v.get("url") if isinstance(v, dict) else v

def html_recipe(soup):
    lis = soup.select("li.wprm-recipe-ingredient")
    if not lis: return None
    og = lambda p: (soup.find("meta", property=p) or {}).get("content", "")
    h1 = soup.find("h1"); tt = soup.select_one('[class*="wprm-recipe-total_time"]')
    tx = tt.get_text(" ").lower() if tt else ""
    h, m = re.search(r"(\d+)\s*hour", tx), re.search(r"(\d+)\s*min", tx)
    return {"name": h1.get_text(" ", strip=True) if h1 else og("og:title"), "image": og("og:image"), "description": og("og:description"),
            "recipeIngredient": [li.get_text(" ", strip=True) for li in lis],
            "recipeInstructions": [x.get_text(" ", strip=True) for x in soup.select(".wprm-recipe-instruction-text")],
            "totalTime": f"PT{int(h.group(1)) if h else 0}H{int(m.group(1)) if m else 0}M" if (h or m) else None}

def scrape_page(url, src):
    soup = BeautifulSoup(get(url), "html.parser")
    rec = find_recipe(soup)
    if not rec or not rec.get("recipeIngredient"): rec = html_recipe(soup)
    if not rec: return None
    return make_recipe(url, src, rec, soup)


def make_recipe(url, src, rec, soup):
    bc = soup.select_one("[class*=breadcrumb]")
    cats = " ".join(a.get_text() for a in bc.find_all("a")) if bc else ""
    cats += " " + str(rec.get("recipeCategory", "")) + " " + str(rec.get("keywords", ""))
    ings = [clean_ingredient(i) for i in rec["recipeIngredient"]]
    title = BeautifulSoup(rec.get("name", ""), "html.parser").get_text()
    n_serv, serv_text = servings_of(rec)
    label = parse_nutrition(soup, rec); est, cov = estimate_nutrition(ings, n_serv)
    nut = {**est, **label}
    return {"url": url, "title": title, "steps": steps_list(rec), "servings": serv_text or str(n_serv), "nut": nut,
            "nutEst": any(k not in label for k in nut), "nutCov": cov, "image": first(rec.get("image")), "source": src["name"],
            "author": src["author"], "minutes": minutes(rec.get("totalTime")),
                        "tags": tag_recipe(title, cats, rec.get("description", ""), ings), "ing": ings, "t": int(time.time()), "desc": short(rec.get("description", "")), "subs": parse_subs(soup), "prep": parse_prep(soup, rec), **parse_flags(rec, title, rec["recipeIngredient"])}

def robots_for(base):
    try:
        r = requests.get(base + "/robots.txt", headers=UA, timeout=20)
        if r.status_code == 200:
            rp = RobotFileParser(); rp.parse(r.text.splitlines())
            return rp, re.findall(r"(?im)^sitemap:\s*(\S+)", r.text)
        log(f"  robots.txt: HTTP {r.status_code} (no rules to follow)")
    except Exception as e: log(f"  robots.txt: {e}")
    return None, []

def rest_urls(base):
    out = {}
    for page in range(1, 80):
        try:
            r = requests.get(base + "/wp-json/wp/v2/posts", params={"per_page": 100, "page": page, "_fields": "link,modified"}, headers=UA, timeout=30)
        except Exception as e: log(f"  REST: {e}"); break
        if r.status_code != 200: log(f"  REST page {page}: HTTP {r.status_code}"); break
        items = r.json()
        if not items: break
        for it in items: out[it["link"]] = it.get("modified", "")
    return out

def discover(base, hints):
    tries = list(dict.fromkeys(hints + [base + p for p in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml")]))
    for sm in tries:
        try:
            u = sitemap_urls(sm); log(f"  sitemap {sm}: {len(u)} URLs")
            if u: return u
        except Exception as e: log(f"  sitemap {sm}: {e}")
    log("  trying the WordPress REST API instead")
    u = rest_urls(base); log(f"  REST API: {len(u)} URLs"); return u

EXTRA = {"High-Protein", "Oil-Free", "Gluten-Free"}   # keyword tags kept alongside the blog's own categories
NOT_RECIPES = {"about", "subscribe", "links", "contact", "recipes", "vegan-meal-plans", "disclaimer", "privacy-policy", "accessibility", "copyright"}

def clean_cat(n):
    n = re.sub(r"\s+", " ", n).strip()
    if re.search(r"recipes?$", n, re.I):
        while re.match(r"(healthy|vegan|easy)\s+", n, re.I): n = re.sub(r"^(healthy|vegan|easy)\s+", "", n, flags=re.I)
        n = re.sub(r"\s+recipes?$", "", n, flags=re.I)
    return n or "Other"

def clean_group(t):
    t = re.sub(r"(?i)^browse by\s+", "", t.strip().rstrip(":")).replace(", &", " &")
    return re.sub(r"(?i)\s+recipes$", "", t)

def parse_categories(html, base):
    """Reads the blog's own category list (e.g. /recipes/) -> [{name, group, url}]."""
    soup = BeautifulSoup(html, "html.parser"); cats = []
    h = next((x for x in soup.find_all("h2") if re.search(r"browse by|dressings|breakfast|holiday", x.get_text(), re.I)), None)
    group = clean_group(h.get_text(" ", strip=True)) if h else None
    for el in (h.find_all_next(["h2", "a"]) if h else []):
        if el.name == "h2":
            t = el.get_text(" ", strip=True)
            if re.search(r"nutrition", t, re.I): break
            group = clean_group(t); continue
        href = el.get("href", "")
        if group and not el.find_parent(["nav", "header", "footer"]) and re.fullmatch(re.escape(base) + r"/[a-z0-9-]+/", href):
            cats.append({"name": clean_cat(el.get_text(" ", strip=True)), "group": group, "url": href})
    return list({c["url"]: c for c in cats}.values())

def category_links(html, base, skip):
    """Recipe links on one category page (stops before the sidebar)."""
    soup = BeautifulSoup(html, "html.parser"); h1 = soup.find("h1"); out = []
    for el in (h1.find_all_next(["h3", "a"]) if h1 else []):
        if el.name == "h3":
            if re.match(r"(?i)\s*(hey|about)", el.get_text()): break
            continue
        href = el.get("href", "").split("#")[0]
        if el.find_parent(["nav", "header", "footer", "aside"]) or href in skip: continue
        m = re.fullmatch(re.escape(base) + r"/([a-z0-9-]+)/", href)
        if m and m.group(1) not in NOT_RECIPES and href not in out: out.append(href)
    return out

def crawl_categories(base, path, source):
    cmap, meta = {}, []
    try: cats = parse_categories(get(base + path), base)
    except Exception as e: log(f"  categories page: {e}"); return cmap, meta
    log(f"  categories page: {len(cats)} categories")
    skip = {c["url"] for c in cats}
    for c in cats:
        time.sleep(DELAY)
        try: links = category_links(get(c["url"]), base, skip)
        except Exception as e: log(f"  category {c['name']}: {e}"); continue
        meta.append({"name": c["name"], "group": c["group"], "source": source})
        for u in links: cmap.setdefault(u, []).append(c["name"])
    log(f"  category pages listed {len(cmap)} links")
    return cmap, meta

import xml.etree.ElementTree as ET

def yt_parse(desc):
    """Pull ingredients and method out of a video description. Returns (intro, ingredients, steps) or None."""
    d = (desc or "").replace("\r", "")
    m = re.search(r"(?im)^[^\w\n]*ingredients?\b[^\n]*$", d)
    if not m: return None
    rest = d[m.end():]
    m2 = re.search(r"(?im)^[^\w\n]*(instructions?|method|directions?|steps?|how to make|preparation)\b[^\n]*$", rest)
    ing_txt, steps_txt = (rest[:m2.start()], rest[m2.end():]) if m2 else (rest, "")
    stop = re.compile(r"(?im)^[^\w\n]*(follow|subscribe|music|chapters|timestamps?|connect|links?|equipment|notes?|nutrition|https?://|#|\d{1,2}:\d{2})")
    cut = lambda t: t[:stop.search(t).start()] if stop.search(t) else t
    ing = []
    for l in cut(ing_txt).splitlines():
        l = re.sub(r"^[\s\u2022\-\u2013*\u25a2\u2713\u00b7]+", "", l).strip()
        if l and not l.endswith(":") and len(l) <= 120: ing.append(l)
    steps = [t for t in (re.sub(r"^[\s\u2022\-\u2013*\d.)]+", "", l).strip() for l in cut(steps_txt).splitlines()) if len(t) > 3]
    return (d[:m.start()].strip(), ing, steps) if len(ing) >= 3 else None

def crawl_youtube(src, recipes):
    cid, key, vids = src["channel_id"], os.environ.get("YOUTUBE_API_KEY", "").strip(), []
    if key:
        tok = None
        while True:
            try:
                data = requests.get("https://www.googleapis.com/youtube/v3/playlistItems", timeout=30,
                    params={"part": "snippet", "playlistId": "UU" + cid[2:], "maxResults": 50, "pageToken": tok, "key": key}).json()
            except Exception as e: log(f"  YouTube API: {e}"); break
            if "items" not in data: log(f"  YouTube API said: {str(data.get('error', data))[:200]}"); break
            for it in data["items"]:
                sn = it["snippet"]; th = sn.get("thumbnails", {}); vid = sn.get("resourceId", {}).get("videoId")
                if vid: vids.append((vid, sn.get("title", ""), sn.get("description", ""), (th.get("high") or th.get("medium") or th.get("default") or {}).get("url", "")))
            tok = data.get("nextPageToken")
            if not tok: break
        log(f"  YouTube API: {len(vids)} videos")
    else: log("  No YOUTUBE_API_KEY set, so only the 15 newest videos can be read (see the setup steps for all of them)")
    if not vids:
        try:
            ns = {"a": "http://www.w3.org/2005/Atom", "m": "http://search.yahoo.com/mrss/", "y": "http://www.youtube.com/xml/schemas/2015"}
            root = ET.fromstring(get(f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"))
            for e in root.findall("a:entry", ns):
                g = e.find("m:group", ns)
                vids.append((e.findtext("y:videoId", "", ns), e.findtext("a:title", "", ns), g.findtext("m:description", "", ns), g.find("m:thumbnail", ns).get("url", "")))
            log(f"  YouTube feed: {len(vids)} videos")
        except Exception as ex: log(f"  YouTube feed: {ex}")
    added, skipped = 0, []
    for vid, title, desc, thumb in vids:
        parsed = yt_parse(desc)
        if not parsed: skipped.append(title); continue
        intro, ing, steps = parsed
        rec = {"name": title, "image": thumb, "description": intro, "recipeIngredient": ing, "recipeInstructions": steps, "totalTime": None}
        r = make_recipe(f"https://www.youtube.com/watch?v={vid}", src, rec, BeautifulSoup("", "html.parser"))
        recipes[r["url"]] = r; added += 1
    log(f"  {added} videos had a recipe in the description; {len(skipped)} had none" + (f" (e.g. {', '.join(skipped[:3])})" if skipped else ""))

def main():
    sources = json.loads((ROOT / "sources.json").read_text())
    recipes = {r["url"]: r for r in json.loads(DATA.read_text())} if DATA.exists() else {}
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    if seen.get("__v") != SCHEMA: seen = {"__v": SCHEMA}
    budget, all_meta = MAX_NEW, []
    def need(u, m):
        if m: return seen.get(u) != m
        if u in recipes: return time.time() - recipes[u].get("t", 0) > 30 * 86400
        return u not in seen
    for src in sources:
        base = src["url"].rstrip("/"); log(f"{src['name']}:")
        if src.get("type") == "youtube": crawl_youtube(src, recipes); continue
        rp, hints = robots_for(base)
        cmap, meta = crawl_categories(base, src["categories_page"], src["name"]) if src.get("categories_page") else ({}, [])
        all_meta += meta
        urls = {u: m for u, m in discover(base, hints).items() if "/web-stories/" not in u}
        for u in cmap: urls.setdefault(u, "")
        if not urls:
            log("  !! Found no pages, so nothing was changed for this blog."); continue
        todo = sorted([(u, m) for u, m in urls.items() if need(u, m)], key=lambda x: x[0] not in cmap)   # recipes from her category pages first
        log(f"  {len(urls)} pages, {len(todo)} to check")
        added = nonrecipe = errors = streak = cools = 0
        for url, mod in todo:
            if budget <= 0: log("  stopped at this run's limit; run it again to continue"); break
            if rp and not rp.can_fetch("PantryRecipesBot", url): continue
            budget -= 1; time.sleep(DELAY)
            try: r = scrape_page(url, src)
            except Exception as e:
                errors += 1; streak += 1
                if errors <= 5: log(f"  error {url}: {e}")
                if streak >= 8:
                    cools += 1
                    if cools > 3: log("  !! The site keeps refusing requests (see errors above). Stopping; run it again later."); break
                    log("  site is refusing requests; pausing 2 minutes"); time.sleep(120); streak = 0
                continue
            streak = 0; seen[url] = mod
            if r: recipes[r["url"]] = r; added += 1
            else:
                nonrecipe += 1
                if nonrecipe <= 6: log(f"  not a recipe: {url}")
        log(f"  added/updated {added} recipes; {nonrecipe} pages were not recipes; {errors} errors")
        if cmap:
            for r in recipes.values():
                if r["source"] == src["name"]:
                    r["cats"] = sorted(set(cmap.get(r["url"], [])))
                    if r["cats"]: r["tags"] = [t for t in r["tags"] if t in EXTRA]
    DATA.write_text(json.dumps(sorted(recipes.values(), key=lambda r: r["title"]), ensure_ascii=False, indent=0))
    SEEN.write_text(json.dumps(seen))
    if all_meta: (ROOT / "data" / "categories.json").write_text(json.dumps(all_meta, ensure_ascii=False, indent=0))
    log(f"Total recipes saved: {len(recipes)}")
    log(f"Estimated nutrition for {sum(1 for r in recipes.values() if r.get('nutEst'))} recipes (average ingredient coverage {round(sum(r.get('nutCov',0) for r in recipes.values())/max(len(recipes),1)*100)}%); nutrition found for {sum(1 for r in recipes.values() if r.get('nut'))} recipes; iron for {sum(1 for r in recipes.values() if 'iron' in r.get('nut', {}))}; times for {sum(1 for r in recipes.values() if r.get('minutes'))}")
    (ROOT / "data" / "status.txt").write_text(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + "\n" + "\n".join(LOG) + "\n")

if __name__ == "__main__": main()
