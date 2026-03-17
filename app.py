from fastapi import FastAPI
import requests
import csv
import io
import os

app = FastAPI()

PRODUCTS_CSV_URL = os.getenv(
    "PRODUCTS_CSV_URL",
    "https://docs.google.com/spreadsheets/d/1dtUYrSy18_D2updwCpVa5wXfgf0hzAXaiQTQqMQnrSc/export?format=csv"
)

@app.get("/")
def root():
    return {"status": "ok"}

@app.get("/list-products")
def list_products():
    resp = requests.get(PRODUCTS_CSV_URL)
    resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(resp.text))
    return list(reader)
