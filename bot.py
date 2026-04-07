#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PropAlertBot - Detecta propiedades nuevas en bancos CR y notifica por Telegram.
Adaptado a las URLs proporcionadas.
"""

import os
import json
import hashlib
import time
import re
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup

# Config desde variables de entorno (GitHub Actions Secrets)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
STATE_FILE = "state.json"
USER_AGENT = "PropAlertBot/1.0 (+https://github.com/tu-usuario/tu-repo)"

HEADERS = {"User-Agent": USER_AGENT}

# Bancos y URLs (tal como las diste)
BANKS = [
    {"name": "Banco Nacional", "url": "https://ventadebienes.bncr.fi.cr/propiedades"},
    {"name": "BCR - Casas", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/Casas?tipo_propiedad=1"},
    {"name": "BCR - Terrenos", "url": "https://ventadebienes.bancobcr.com/wps/portal/bcrb/bcrbienes/bienes/terrenos?&tipo_propiedad=3"},
    {"name": "Banco Popular", "url": "https://srv.bancopopular.fi.cr/Wb_BA_SharepointU/"},
    {"name": "BAC", "url": "https://www.baccredomatic.com/es-cr/personas/viviendas-adjudicadas"},
    {"name": "Scotiabank", "url": "https://www.davibank.cr/homeshow/casas.aspx"},
    {"name": "BienesAdjudicadosCR", "url": "https://bienesadjudicadoscr.com/propiedades/"}
]

# -------------------------
# Utilidades
# -------------------------
def load_state(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"seen": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path: str, state: Dict[str, Any]):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def make_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()

def send_telegram(token: str, chat_id: str, text: str):
    if not token or not chat_id:
        print("Telegram token/chat_id no configurados; omitiendo envío.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"   
payload = {
    "chat_id": chat_id,
    "text": text,
    "parse_mode": "Markdown"
}
    try:
        r = requests.post(url, data=payload, timeout=15)
        r.raise_for_status()
        print("Telegram enviado.")
    except Exception as e:
        print("Error enviando Telegram:", e)

def safe_get(url: str, timeout: int = 20) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print("Error GET", url, e)
        return ""

# -------------------------
# Extracción simple de campos desde texto
# -------------------------

def extract_size(text: str) -> str:
    m = re.search(r"(\d{1,5})\s*(m2|m²)", text, re.IGNORECASE)
    if m:
        return f"{m.group(1)} m²"
    return ""

def extract_price(text: str) -> str:
    m = re.search(r"(₡\s?[\d\.,]+|\bCRC\s?[\d\.,]+|\$\s?[\d\.,]+)", text)
    if m:
        return m.group(1)
    m2 = re.search(r"(\d{1,3}(?:[.,]\d{3})+)", text)
    if m2:
        return m2.group(1)
    return ""

def extract_location(text: str) -> str:
    provinces = ["Cartago", "San José", "San Jose", "Heredia", "Alajuela", "Puntarenas", "Limón", "Limon"]
    for p in provinces:
        if p.lower() in text.lower():
            return p
    m = re.search(r"Ubicaci[oó]n[:\-]\s*([^,;\n]+)", text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # heurística: buscar "Provincia" o "Cantón"
    m2 = re.search(r"(Provincia|Cant[oó]n)[:\-]\s*([^,;\n]+)", text, re.IGNORECASE)
    if m2:
        return m2.group(2).strip()
    return ""

# -------------------------
# Parsers específicos (heurísticos)
# -------------------------
def parse_bn(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    # Heurística: buscar tarjetas con clase que contenga 'card' o enlaces a detalle
    cards = soup.find_all(lambda tag: tag.name in ["article", "div"] and tag.get("class") and any("card" in c.lower() or "prop" in c.lower() for c in tag.get("class")))
    if not cards:
        # fallback: buscar enlaces con '/propiedad' o '/detalle'
        anchors = soup.find_all("a", href=True)
        for a in anchors:
            href = a["href"]
            if any(k in href.lower() for k in ["/prop", "/detalle", "/ficha", "/inmueble"]):
                title = a.get_text(strip=True) or a.get("title") or ""
                full = href if href.startswith("http") else requests.compat.urljoin(url, href)
                snippet = a.parent.get_text(" ", strip=True) if a.parent else title
                items.append({
                    "url": full,
                    "title": title,
                    "location": extract_location(snippet),
                    "size": extract_size(snippet),
                    "price": extract_price(snippet),
                    "id": make_id(full + title)
                })
        return items
    for c in cards:
        a = c.find("a", href=True)
        title = (c.get_text(" ", strip=True)[:120]) if not a else (a.get_text(" ", strip=True) or a.get("title") or "")
        href = a["href"] if a else ""
        full = href if href.startswith("http") else requests.compat.urljoin(url, href)
        snippet = c.get_text(" ", strip=True)
        items.append({
            "url": full or url,
            "title": title,
            "location": extract_location(snippet),
            "size": extract_size(snippet),
            "price": extract_price(snippet),
            "id": make_id(full + title)
        })
    return items

def parse_bcr(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    # BCR suele listar tarjetas con enlaces a detalle; buscar enlaces que contengan 'detalle' o 'ficha'
    anchors = soup.find_all("a", href=True)
    seen = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["/detalle", "/ficha", "bienes"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = a.get_text(" ", strip=True) or ""
            parent = a.parent
            snippet = parent.get_text(" ", strip=True) if parent else title
            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title)
            })
    return items

def parse_popular(url: str) -> List[Dict[str, str]]:
    # SharePoint / portal: muchas veces requiere JS. Intentamos extraer enlaces y títulos.
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    seen = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["/sites/", "/bienes", "/propiedades", "/detalle"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = a.get_text(" ", strip=True) or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title)
            })
    return items

def parse_bac(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    # Buscar tarjetas con clase 'card' o enlaces que contengan 'vivienda' o 'detalle'
    anchors = soup.find_all("a", href=True)
    seen = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["vivienda", "detalle", "/propiedad", "/inmueble"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = a.get_text(" ", strip=True) or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title)
            })
    return items

def parse_scotiabank(url: str) -> List[Dict[str, str]]:
    # URL dada apunta a davibank; heurística genérica
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a", href=True)
    seen = set()
    for a in anchors:
        href = a["href"]
        if any(k in href.lower() for k in ["casas", "detalle", "propiedad", "ficha"]):
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = a.get_text(" ", strip=True) or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title)
            })
    return items

def parse_bienesadjudicados(url: str) -> List[Dict[str, str]]:
    html = safe_get(url)
    items = []
    if not html:
        return items
    soup = BeautifulSoup(html, "html.parser")
    # Sitio WordPress: buscar entradas / tarjetas con clase 'property' o enlaces a '/propiedades/'
    anchors = soup.find_all("a", href=True)
    seen = set()
    for a in anchors:
        href = a["href"]
        if "/propiedades/" in href or "/propiedad/" in href or "propiedades" in href.lower():
            full = href if href.startswith("http") else requests.compat.urljoin(url, href)
            if full in seen:
                continue
            seen.add(full)
            title = a.get_text(" ", strip=True) or ""
            snippet = a.parent.get_text(" ", strip=True) if a.parent else title
            items.append({
                "url": full,
                "title": title,
                "location": extract_location(snippet),
                "size": extract_size(snippet),
                "price": extract_price(snippet),
                "id": make_id(full + title)
            })
    return items

PARSERS = {
    "Banco Nacional": parse_bn,
    "BCR - Casas": parse_bcr,
    "BCR - Terrenos": parse_bcr,
    "Banco Popular": parse_popular,
    "BAC": parse_bac,
    "Scotiabank": parse_scotiabank,
    "BienesAdjudicadosCR": parse_bienesadjudicados
}

# -------------------------
# Flujo principal
# -------------------------
def main():
    state = load_state(STATE_FILE)
    seen = set(state.get("seen", []))
    new_seen = set(seen)
    new_items = []

    for bank in BANKS:
        name = bank["name"]
        url = bank["url"]
        print(f"Comprobando {name} -> {url}")
        parser = PARSERS.get(name, None)
        if parser:
            items = parser(url)
        else:
            # fallback genérico
            items = []
        print(f"  Candidatos encontrados: {len(items)}")
        for it in items:
            item_id = it.get("id") or make_id(it.get("url", "") + it.get("title", ""))
            if item_id in seen:
                continue
            new_items.append({"bank": name, **it, "id": item_id})
            new_seen.add(item_id)

    # Enviar notificaciones por Telegram
    for it in new_items:
        msg = (
            "🚨 Propiedad nueva detectada\n"
            f"🏦 Banco: {it.get('bank')}\n"
            f"📍 Ubicación: {it.get('location') or 'No disponible'}\n"
            f"📐 Tamaño: {it.get('size') or 'No disponible'}\n"
            f"💰 Precio: {it.get('price') or 'No disponible'}\n"
            f"🔗 Ver detalles: {it.get('url')}\n"
        )
        print("Enviando:", it.get("url"))
        send_telegram(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, msg)
        time.sleep(1)

    # Actualizar estado si hay nuevos
    if new_seen != seen:
        state["seen"] = list(new_seen)
        save_state(STATE_FILE, state)
        print("Estado actualizado:", len(new_seen) - len(seen), "nuevos.")
    else:
        print("No hay nuevos items.")

if __name__ == "__main__":
    main()
