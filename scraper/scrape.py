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
SCHEMA = 4                                      # bump to re-fetch everything
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
    t = re.sub(r"\b(with|plus|such as|like|for|divided|at room temperature|on hand|what you have|or more)\b.*", "", t).strip()
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
            "tags": tag_recipe(title, cats, rec.get("description", ""), ings), "ing": ings, "t": int(time.time()), "desc": short(rec.get("description", "")), "subs": parse_subs(soup), "prep": parse_prep(soup, rec), "nut": parse_nutrition(soup, rec), **parse_flags(rec, title, rec["recipeIngredient"])}

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
        rp, hints = robots_for(base)
        cmap, meta = crawl_categories(base, src["categories_page"], src["name"]) if src.get("categories_page") else ({}, [])
        all_meta += meta
        urls = {u: m for u, m in discover(base, hints).items() if "/web-stories/" not in u}
        for u in cmap: urls.setdefault(u, "")
        if not urls:
            log("  !! Found no pages, so nothing was changed for this blog."); continue
        todo = [(u, m) for u, m in urls.items() if need(u, m)]
        log(f"  {len(urls)} pages, {len(todo)} to check")
        added = nonrecipe = errors = streak = 0
        for url, mod in todo:
            if budget <= 0: log("  stopped at this run's limit; run it again to continue"); break
            if rp and not rp.can_fetch("PantryRecipesBot", url): continue
            budget -= 1; time.sleep(DELAY)
            try: r = scrape_page(url, src)
            except Exception as e:
                errors += 1; streak += 1
                if errors <= 5: log(f"  error {url}: {e}")
                if streak >= 8: log("  !! The site keeps refusing requests (see errors above). Stopping."); break
                continue
            streak = 0; seen[url] = mod
            if r: recipes[r["url"]] = r; added += 1
            else: nonrecipe += 1
        log(f"  added/updated {added} recipes; {nonrecipe} pages were not recipes; {errors} errors")
        live = set(urls)  # drop recipes removed from the blog
        recipes = {u: r for u, r in recipes.items() if r["source"] != src["name"] or u in live}
        if cmap:
            for r in recipes.values():
                if r["source"] == src["name"]:
                    r["cats"] = sorted(set(cmap.get(r["url"], [])))
                    if r["cats"]: r["tags"] = [t for t in r["tags"] if t in EXTRA]
    DATA.write_text(json.dumps(sorted(recipes.values(), key=lambda r: r["title"]), ensure_ascii=False, indent=0))
    SEEN.write_text(json.dumps(seen))
    if all_meta: (ROOT / "data" / "categories.json").write_text(json.dumps(all_meta, ensure_ascii=False, indent=0))
    log(f"Total recipes saved: {len(recipes)}")
    log(f"Nutrition found for {sum(1 for r in recipes.values() if r.get('nut'))} recipes; iron for {sum(1 for r in recipes.values() if 'iron' in r.get('nut', {}))}; times for {sum(1 for r in recipes.values() if r.get('minutes'))}")
    (ROOT / "data" / "status.txt").write_text(time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()) + "\n" + "\n".join(LOG) + "\n")

if __name__ == "__main__": main()
