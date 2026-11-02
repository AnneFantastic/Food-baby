"""Reads each blog in sources.json, finds recipes, writes data/recipes.json.
Only structured facts are stored (title, ingredients, tags, link) - the method
stays on the original blog. Safe to re-run: unchanged posts are skipped."""
import json, re, time, pathlib, os
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA, SEEN = ROOT / "data" / "recipes.json", ROOT / "data" / "seen.json"
UA = {"User-Agent": "PantryRecipesBot/1.0 (personal recipe finder; respects robots.txt)"}
SCHEMA = 2                                      # bump to re-fetch everything
MAX_NEW = int(os.environ.get("MAX_NEW", 400))   # posts fetched per run
DELAY = 1.0                                     # seconds between page requests

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
    r = requests.get(url, headers=UA, timeout=30); r.raise_for_status(); return r.text

def sitemap_urls(url, depth=0):
    out = {}
    xml = get(url)
    for blk in re.findall(r"<(?:url|sitemap)>.*?</(?:url|sitemap)>", xml, re.S):
        loc = re.search(r"<loc>(.*?)</loc>", blk).group(1).strip()
        mod = (re.search(r"<lastmod>(.*?)</lastmod>", blk) or [None, ""])[1]
        if loc.endswith(".xml"):
            if depth < 2 and not re.search(r"page|categor|tag|author|attachment|product", loc):
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

def parse_qty(text):
    """'1 ½ cups dried red lentils' -> (1.5, 'cup'). Ranges use the upper number."""
    raw = re.sub(r"\(.*?\)", "", text.lower().replace("▢", "")).replace("–", "-")
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
    t = re.sub(r"\(.*?\)", "", text.lower().replace("▢", ""))
    optional = "optional" in text.lower()
    t = re.sub(r"\bor to taste\b|\boptional\b|\bto taste\b", "", t).split(",")[0]
    t = re.split(r"\babout\b", t)[0]
    names = []
    for part in re.split(r"\bor\b", t):
        toks = part.split()
        while toks and (re.fullmatch(r"[\d¼½¾⅓⅔⅛/.\-–]+", toks[0]) or re.fullmatch(UNITS, toks[0].strip(".")) or toks[0] == "of"):
            toks.pop(0)
        if toks: names.append(" ".join(toks))
    q, u = parse_qty(text)
    mc = re.search(r"\babout (\d+) (?:small|medium|large|whole|big)\b", text.lower())
    return {"text": text.strip(), "names": names or [t.strip()], "opt": optional, "q": q, "u": u, "c": int(mc.group(1)) if mc else None}

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

def scrape_page(url, src):
    soup = BeautifulSoup(get(url), "html.parser")
    rec = find_recipe(soup)
    if not rec or not rec.get("recipeIngredient"): return None
    bc = soup.select_one("[class*=breadcrumb]")
    cats = " ".join(a.get_text() for a in bc.find_all("a")) if bc else ""
    cats += " " + str(rec.get("recipeCategory", "")) + " " + str(rec.get("keywords", ""))
    ings = [clean_ingredient(i) for i in rec["recipeIngredient"]]
    title = BeautifulSoup(rec.get("name", ""), "html.parser").get_text()
    return {"url": url, "title": title, "image": first(rec.get("image")), "source": src["name"],
            "author": src["author"], "minutes": minutes(rec.get("totalTime")),
            "servings": str((rec.get("recipeYield") or [""])[0] if isinstance(rec.get("recipeYield"), list) else rec.get("recipeYield") or ""),
            "tags": tag_recipe(title, cats, rec.get("description", ""), ings), "ing": ings, "desc": short(rec.get("description", "")), "subs": parse_subs(soup), "prep": parse_prep(soup, rec)}

def main():
    sources = json.loads((ROOT / "sources.json").read_text())
    recipes = {r["url"]: r for r in json.loads(DATA.read_text())} if DATA.exists() else {}
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    if seen.get("__v") != SCHEMA: seen = {"__v": SCHEMA}
    budget = MAX_NEW
    for src in sources:
        base = src["url"].rstrip("/")
        rp = RobotFileParser(base + "/robots.txt")
        try: rp.read()
        except Exception: pass
        urls = {}
        for sm in ("/sitemap_index.xml", "/wp-sitemap.xml", "/sitemap.xml"):
            try: urls = sitemap_urls(base + sm); break
            except Exception: continue
        print(f"{src['name']}: {len(urls)} URLs in sitemap")
        for url, mod in urls.items():
            if seen.get(url) == mod and (url in recipes or mod): continue
            if budget <= 0: break
            if not rp.can_fetch(UA["User-Agent"], url): continue
            budget -= 1; time.sleep(DELAY)
            try: r = scrape_page(url, src)
            except Exception as e: print("skip", url, e); continue
            seen[url] = mod
            if r: recipes[r["url"]] = r; print("added", r["title"])
        live = set(urls)  # drop recipes removed from the blog
        recipes = {u: r for u, r in recipes.items() if r["source"] != src["name"] or u in live}
    DATA.write_text(json.dumps(sorted(recipes.values(), key=lambda r: r["title"]), ensure_ascii=False, indent=0))
    SEEN.write_text(json.dumps(seen))
    print(f"Done: {len(recipes)} recipes")

if __name__ == "__main__": main()
