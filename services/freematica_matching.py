# -*- coding: utf-8 -*-
"""Normalización de nombres y fuzzy-matching entre proveedores Finia
(finia.ocr.vendor) y proveedores Freematica (freematica.provider). Mismo
algoritmo que finia_facturaweb/services/facturaweb_matching.py, para que el
comportamiento sea consistente entre ambas integraciones."""
import difflib
import re
import unicodedata


def normalize_name(value):
    if not value:
        return ''
    text = unicodedata.normalize('NFKD', str(value))
    text = ''.join(char for char in text if not unicodedata.combining(char))
    text = text.upper()
    text = re.sub(r'[^A-Z0-9 ]+', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def normalize_nif(value):
    if not value:
        return ''
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def best_match(query_name, candidates, threshold):
    """candidates: iterable de (key, name). Devuelve (best_key, best_score);
    best_key es None si ningún candidato alcanza `threshold`."""
    normalized_query = normalize_name(query_name)
    best_key = None
    best_score = 0.0
    if not normalized_query:
        return None, 0.0
    for key, name in candidates:
        score = difflib.SequenceMatcher(None, normalized_query, normalize_name(name)).ratio()
        if score > best_score:
            best_score = score
            best_key = key
    if best_score < threshold:
        return None, best_score
    return best_key, best_score
