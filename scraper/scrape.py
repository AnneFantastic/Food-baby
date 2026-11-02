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

def clean_ingredient(text):
    t = re.sub(r"\(.*?\)", "", text.lower().replace("▢", ""))
    optional = "optional" in t
    t = re.sub(r"\bor to taste\b|\boptional\b|\bto taste\b", "", t).split(",")[0]
    names = []
    for part in re.split(r"\bor\b", t):
        toks = part.split()
        while toks and (re.fullmatch(r"[\d¼½¾⅓⅔⅛/.\-–]+", toks[0]) or re.fullmatch(UNITS, toks[0].strip(".")) or toks[0] == "of"):
            toks.pop(0)
        if toks: names.append(" ".join(toks))
    return {"text": text.strip(), "names": names or [t.strip()], "opt": optional}

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

def tag_recipe(title, cats, desc, ings):
    hay = f" {title} {cats} {desc} ".lower()
    tags = [t for t, kws in TAGS.items() if any(k in hay for k in kws)]
    ihay = (title + " " + " ".join(i["text"] for i in ings)).lower()
    return tags + [t for t, kws in INGR_TAGS.items() if any(k in ihay for k in kws)]

def minutes(iso):
    m = re.match(r"P(?:\d+D)?T?(?:(\d+)H)?(?:(\d+)M)?", iso or "")
    return (int(m.group(1) or 0) * 60 + int(m.group(2) or 0)) or None if m else None

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
            "tags": tag_recipe(title, cats, rec.get("description", ""), ings), "ing": ings, "subs": parse_subs(soup)}

def main():
    sources = json.loads((ROOT / "sources.json").read_text())
    recipes = {r["url"]: r for r in json.loads(DATA.read_text())} if DATA.exists() else {}
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
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
