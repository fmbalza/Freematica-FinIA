# -*- coding: utf-8 -*-
"""Cliente REST/JSON para la API de Freematica (e-Satellite).

No usa una librería de cliente HTTP generada desde el OpenAPI: construye los
requests a mano con `requests`, igual de simple que
`facturaweb_soap_client.py` en el módulo hermano `finia_facturaweb`, pero para
JSON en vez de SOAP.

Documentación de referencia: `Documentacion_API_Freematica_Asientos_Contables.md`
(raíz de `Finia Front-End`). Puntos aún sin confirmar contra el entorno real
(ver sección 10 del documento): host de producción, y si `LINEAS`/`IVA`/
`CARTERA`/`ANALITICA` deben ir como array JSON nativo o como string con el
array serializado — por eso `_serialize_lineas_if_needed` soporta ambas
variantes vía `config['lineas_as_json_string']`.
"""
import copy
import json
import logging
from datetime import datetime, timezone

import requests

_logger = logging.getLogger(__name__)

LOGIN_PATH = '/ppre/v2/control/entrada'
IMPORT_ASIENTOS_PATH = '/pcon/v2/import-asientos'
EXPORT_ASIENTOS_PATH = '/pcon/v2/export-asientos'

# Claves, dentro de una línea de asiento, que representan listas de objetos y
# que deben serializarse también si `lineas_as_json_string` está activo.
_LINE_ARRAY_KEYS = ('IVA', 'CARTERA', 'ANALITICA')


class FreematicaError(Exception):
    def __init__(self, mensaje, operation=None, status_code=None):
        self.mensaje = mensaje
        self.operation = operation
        self.status_code = status_code
        super().__init__(mensaje)


def _base_url(config):
    host = (config.get('host') or '').rstrip('/')
    if not host:
        raise FreematicaError('Freematica: falta configurar el host de la API.')
    return host


def _build_headers(config, token=None, include_content_type=True):
    headers = {}
    if include_content_type:
        headers['Content-Type'] = 'application/json'
    if config.get('x_auth_organization'):
        headers['X-Auth-Organization'] = str(config['x_auth_organization'])
    if config.get('x_auth_company'):
        headers['X-Auth-Company'] = str(config['x_auth_company'])
    # `token=None` (default, no lo pasó el caller) -> usar el de la config.
    # `token=''` (pasado explícitamente, p.ej. por `login`) -> a propósito
    # sin token, NO caer de vuelta al de la config con un `or`.
    auth_token = config.get('access_token') if token is None else token
    if auth_token:
        headers['X-Auth-Token'] = auth_token
    # No documentadas oficialmente en la especificación OpenAPI, pero las
    # credenciales de prueba de Servinet las incluyen — se envían si están
    # configuradas, se omiten si no.
    if config.get('x_auth_session'):
        headers['X-Auth-Session'] = str(config['x_auth_session'])
    if config.get('x_auth_app'):
        headers['X-Auth-App'] = str(config['x_auth_app'])
    headers['Language'] = config.get('language') or 'es'
    return headers


def _request(config, method, path, operation, params=None, json_body=None, token=None):
    url = _base_url(config) + path
    timeout = config.get('request_timeout') or 20
    headers = _build_headers(config, token=token, include_content_type=json_body is not None)
    try:
        response = requests.request(
            method, url, headers=headers, params=params,
            data=json.dumps(json_body) if json_body is not None else None,
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise FreematicaError(
            'Error de conexión con Freematica (%s): %s' % (operation, error), operation,
        ) from error

    if response.status_code not in (200, 201):
        _logger.error(
            'Freematica %s respondió HTTP %s: %s', operation, response.status_code, response.text[:2000],
        )
        raise FreematicaError(
            'Freematica respondió HTTP %s en %s: %s' % (response.status_code, operation, response.text[:500]),
            operation, status_code=response.status_code,
        )

    if not response.text:
        return {}
    try:
        return response.json()
    except ValueError as error:
        raise FreematicaError(
            'Respuesta inválida (no-JSON) de Freematica en %s: %s' % (operation, error), operation,
        ) from error


def login(config):
    """POST /ppre/v2/control/entrada — intercambia username/password por un
    access_token (VoLoginMapp.loginInfo.access_token). Requiere
    `login_username`/`login_password` en `config`."""
    username = config.get('login_username')
    password = config.get('login_password')
    if not username or not password:
        raise FreematicaError(
            'Freematica: no hay login_username/login_password configurados para renovar el token.',
            'login',
        )
    body = {
        'username': username,
        'password': password,
        'refresh_session': config.get('x_auth_session') or '',
    }
    data = _request(config, 'POST', LOGIN_PATH, 'login', json_body=body, token='')
    login_info = data.get('loginInfo') if isinstance(data, dict) else None
    access_token = (login_info or {}).get('access_token') or data.get('access_token')
    if not access_token:
        raise FreematicaError(
            'Freematica: el login no devolvió access_token (respuesta: %s)' % json.dumps(data)[:500],
            'login',
        )
    return {
        'access_token': access_token,
        'obtained_at': datetime.now(timezone.utc),
    }


def _serialize_lineas_if_needed(payload, as_string):
    if not as_string:
        return payload
    payload = copy.deepcopy(payload)
    lineas = payload.get('LINEAS')
    if not isinstance(lineas, list):
        return payload
    for linea in lineas:
        for key in _LINE_ARRAY_KEYS:
            if key in linea and isinstance(linea[key], list):
                linea[key] = json.dumps(linea[key], ensure_ascii=False)
    payload['LINEAS'] = json.dumps(lineas, ensure_ascii=False)
    return payload


def import_asientos(config, asiento_payload, token=None):
    """POST /pcon/v2/import-asientos — inserta un asiento contable en
    borrador (VoAsientoBorrador). Devuelve el eco del asiento creado."""
    body = _serialize_lineas_if_needed(asiento_payload, config.get('lineas_as_json_string'))
    return _request(config, 'POST', IMPORT_ASIENTOS_PATH, 'import-asientos', json_body=body, token=token)


def export_asientos(config, empresa, cal, periodo=None, rquery=None, page=None, items=None, token=None):
    """GET /pcon/v2/export-asientos — consulta asientos ya contabilizados."""
    params = {'empresa': empresa, 'cal': cal}
    if periodo is not None:
        params['periodo'] = periodo
    if rquery:
        params['rQuery'] = rquery
    if page is not None:
        params['page'] = page
    if items is not None:
        params['items'] = items
    return _request(config, 'GET', EXPORT_ASIENTOS_PATH, 'export-asientos', params=params, token=token)


def test_connection(config, token=None):
    """Prueba de conectividad de solo lectura: un `export_asientos` mínimo
    con la empresa configurada y el año en curso como calendario/ejercicio."""
    empresa = config.get('x_auth_company')
    if not empresa:
        raise FreematicaError('Freematica: falta configurar X-Auth-Company para probar la conexión.')
    current_year = datetime.now(timezone.utc).year
    return export_asientos(config, empresa=empresa, cal=current_year, items=1, token=token)
