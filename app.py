from fastapi import FastAPI, HTTPException
import requests, csv, io, os, re

app = FastAPI()

PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv"
)

STOPWORDS = {"the","and","for","with","from","your","you","our","this","that","soil","organic","liquid","natural","plants","plant","garden","lawn"}

def normalize(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()

def load_products():
    r = requests.get(PRODUCTS_CSV_URL)
    r.raise_for_status()
    reader = csv.DictReader(io.StringIO(r.text))
    return [row for row in reader]

def find_product(key):
    products = load_products()
    key = key.lower()
    for p in products:
        if p.get("Product_ID","").lower() == key or p.get("SKU","").lower() == key:
            return p
    raise HTTPException(status_code=404, detail="Product not found")

def generate_keywords(product):
    keywords = []

    title = normalize(product.get("Title",""))
    words = [w for w in title.split() if w not in STOPWORDS and len(w) > 2]

    # base phrases
    keywords.append(title)

    # n-grams
    for i in range(len(words)):
        if i+1 < len(words):
            keywords.append(words[i] + " " + words[i+1])
        if i+2 < len(words):
            keywords.append(words[i] + " " + words[i+1] + " " + words[i+2])

    # sheet keywords
    for field in ["Keywords","Research_Keywords"]:
        if product.get(field):
            parts = re.split(r"[,\n;]", product[field])
            keywords.extend([normalize(p) for p in parts if p.strip()])

    # category boost
    category = normalize(product.get("Category",""))
    if "dog" in category:
        keywords += ["dog urine lawn repair","dog urine neutralizer"]
    if "lawn" in category or "pasture" in category:
        keywords += ["lawn fertilizer","grass fertilizer","pasture fertilizer"]

    # clean + dedupe
    seen = set()
    final = []
    for k in keywords:
        if k and k not in seen:
            seen.add(k)
            final.append(k)

    return final[:30]

@app.get("/")
def root():
    return {"status":"ok"}

@app.get("/list-products")
def list_products():
    return load_products()

@app.get("/product/{key}")
def product(key: str):
    return find_product(key)

@app.get("/generate-keywords/{key}")
def keywords(key: str):
    p = find_product(key)
    return {
        "product": p.get("Title"),
        "keywords": generate_keywords(p)
    }

@app.post("/create-campaign-from-product")
def create_campaign(data: dict):
    key = data.get("product_id") or data.get("sku")
    if not key:
        raise HTTPException(status_code=400, detail="Provide product_id or sku")

    p = find_product(key)

    return {
        "message": "Campaign ready (simulation)",
        "product": p.get("Title"),
        "asin": p.get("ASIN"),
        "sku": p.get("SKU"),
        "suggested_keywords": generate_keywords(p)
    }
