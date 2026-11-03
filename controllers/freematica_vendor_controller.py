# -*- coding: utf-8 -*-
"""Proveedor de Freematica de una factura: ver con qué ficha se ha
emparejado, buscar en el catálogo, asignarlo a mano y deshacerlo. Mismo
flujo que la sección "Proveedor" de García Riera con Praxis.

La ficha de Freematica se guarda en el proveedor de Finia
(finia.ocr.vendor), así que vale para todas sus facturas.

Como el resto de rutas del módulo, responden SIEMPRE HTTP 200 (ver
freematica_accounts_controller.py)."""
import json
import logging

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response

_logger = logging.getLogger(__name__)


def _body():
    try:
        return json.loads(request.httprequest.data.decode('utf-8')) if request.httprequest.data else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _error(message):
    return json_response({'success': False, 'error': message}, 200)


def _invoice_vendor(invoice_id):
    invoice = request.env['finia.invoice'].sudo().browse(invoice_id).exists()
    if not invoice:
        raise UserError('La factura no existe.')
    if not invoice.ocr_vendor_id:
        raise UserError('La factura no tiene proveedor leído por el OCR: corrígelo antes de elegir '
                        'su ficha de Freematica.')
    return invoice.ocr_vendor_id


class FreematicaVendorController(http.Controller):

    @http.route('/api/v1/freematica/providers/search', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def search_providers(self, q='', limit=20, **kwargs):
        """GET /api/v1/freematica/providers/search?q= — por código, NIF o nombre."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            limit = max(1, min(int(limit or 20), 50))
            providers = request.env['freematica.provider'].sudo().freematica_search(q, limit=limit)
            return json_response({'success': True, 'data': [p._freematica_api_dict() for p in providers]})
        except Exception as error:
            _logger.error('Freematica providers/search error: %s', error)
            return _error(str(error))

    @http.route('/api/v1/invoices/<int:invoice_id>/freematica-provider', type='http', auth='public',
                methods=['GET', 'POST', 'OPTIONS'], csrf=False)
    def invoice_provider(self, invoice_id, **kwargs):
        """GET: ficha de Freematica del proveedor de la factura.
        POST {"provider_id": N}: asignarla a mano (queda fija)."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            vendor = _invoice_vendor(invoice_id)
            if request.httprequest.method == 'POST':
                provider_id = _body().get('provider_id')
                if not provider_id:
                    return _error('Elige un proveedor de Freematica.')
                vendor.action_freematica_set_manual_match(int(provider_id))
            elif vendor.freematica_match_state == 'no_intentado':
                vendor._freematica_resolve_provider()
            return json_response({'success': True, 'data': vendor._freematica_api_dict()})
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.error('Freematica invoice provider error (factura %s): %s', invoice_id, error)
            return _error(str(error))

    @http.route('/api/v1/invoices/<int:invoice_id>/freematica-provider/restore', type='http',
                auth='public', methods=['POST', 'OPTIONS'], csrf=False)
    def restore_provider(self, invoice_id, **kwargs):
        """POST: deshace la última asignación manual."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            vendor = _invoice_vendor(invoice_id)
            vendor.action_freematica_restore_previous()
            return json_response({'success': True, 'data': vendor._freematica_api_dict()})
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.error('Freematica restore provider error (factura %s): %s', invoice_id, error)
            return _error(str(error))

    @http.route('/api/v1/freematica/vendors/<int:vendor_id>/provider', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def vendor_provider(self, vendor_id, **kwargs):
        """POST {"provider_id": N} o {"restore": true}: lo mismo desde el panel
        de avisos, que trabaja por proveedor y no por factura."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            vendor = request.env['finia.ocr.vendor'].sudo().browse(vendor_id).exists()
            if not vendor:
                return _error('El proveedor no existe.')
            body = _body()
            if body.get('restore'):
                vendor.action_freematica_restore_previous()
            elif body.get('provider_id'):
                vendor.action_freematica_set_manual_match(int(body['provider_id']))
            else:
                return _error('Elige un proveedor de Freematica.')
            return json_response({'success': True, 'data': vendor._freematica_api_dict()})
        except UserError as error:
            return _error(str(error))
        except Exception as error:
            _logger.error('Freematica vendor provider error (proveedor %s): %s', vendor_id, error)
            return _error(str(error))
