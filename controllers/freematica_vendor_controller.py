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

from odoo import fields, http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response

_logger = logging.getLogger(__name__)


def _body():
    try:
        return json.loads(request.httprequest.data.decode('utf-8')) if request.httprequest.data else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def _config(kwargs):
    """Configuración de Freematica de la petición: la de `empresa_id` (la empresa
    que el front tiene activa) o, sin él, la más antigua (Servinet)."""
    Config = request.env['freematica.config'].sudo()
    empresa_id = kwargs.get('empresa_id')
    return Config.get_config_for(int(empresa_id)) if empresa_id else Config._current()


def _error(message):
    return json_response({'success': False, 'error': message}, 200)


def _invoice_vendor(invoice_id):
    invoice = request.env['finia.invoice'].sudo().browse(invoice_id).exists()
    if not invoice:
        raise UserError('La factura no existe.')
    if not invoice.ocr_vendor_id:
        raise UserError('La factura no tiene proveedor leído por el OCR: corrígelo antes de elegir '
                        'su ficha de Freematica.')
    return invoice._freematica_with_config().ocr_vendor_id


class FreematicaVendorController(http.Controller):

    @http.route('/api/v1/freematica/providers/search', type='http', auth='public',
                methods=['GET', 'OPTIONS'], csrf=False)
    def search_providers(self, q='', limit=20, **kwargs):
        """GET /api/v1/freematica/providers/search?q= — por código, NIF o nombre."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            limit = max(1, min(int(limit or 20), 100))
            config = _config(kwargs)
            providers = request.env['freematica.provider'].sudo().with_context(
                freematica_config_id=config.id).freematica_search(q, limit=limit)
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
            config = _config(kwargs)
            vendor = request.env['finia.ocr.vendor'].sudo().with_context(
                freematica_config_id=config.id).browse(vendor_id).exists()
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


_VENDOR_FILTERS = {
    'sin_identificar': [('freematica_cod_aux', '=', False)],
    'revisar': [('freematica_match_state', '=', 'coincidencia_automatica')],
    'varios': [('freematica_match_state', '=', 'varios_nif')],
    'manual': [('freematica_match_state', '=', 'coincidencia_manual')],
    'todos': [],
}
_EDITABLE = ('default_accounting_account', 'freematica_tipo_impuesto', 'freematica_has_irpf')


def _invoice_counts(vendor_ids, config=None):
    """{('pending'|'sent', vendor_id): nº de facturas}."""
    counts = {}
    if not vendor_ids:
        return counts
    Invoice = request.env['finia.invoice'].sudo()
    sent = Invoice._read_group([('ocr_vendor_id', 'in', vendor_ids), ('freematica_state', '=', 'enviado')],
                               ['ocr_vendor_id'], ['__count'])
    for vendor, count in sent:
        counts[('sent', vendor.id)] = count
    pending = Invoice._read_group(
        request.env['finia.ocr.vendor'].sudo()._freematica_pending_invoices_domain(config)
        + [('ocr_vendor_id', 'in', vendor_ids)], ['ocr_vendor_id'], ['__count'])
    for vendor, count in pending:
        counts[('pending', vendor.id)] = count
    return counts


class FreematicaVendorSettingsController(http.Controller):

    @http.route('/api/v1/freematica/vendors', type='http', auth='public', methods=['GET', 'OPTIONS'], csrf=False)
    def list_vendors(self, filter='todos', q='', limit=50, offset=0, **kwargs):
        """GET /api/v1/freematica/vendors?filter=&q=&limit=&offset=
        Proveedores de Finia con su ficha de Freematica (Configuración >
        Proveedores). filter: sin_identificar | revisar | varios | manual | todos."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            config = _config(kwargs)
            Vendor = request.env['finia.ocr.vendor'].sudo().with_context(freematica_config_id=config.id)
            limit = max(1, min(int(limit or 50), 200))
            offset = max(0, int(offset or 0))
            base = []
            q = (q or '').strip()
            if q:
                base = ['|', '|', '|', ('name', 'ilike', q), ('vat', 'ilike', q), ('nif', 'ilike', q),
                        ('freematica_cod_aux', 'ilike', q)]
            domain = base + _VENDOR_FILTERS.get(filter, [])
            total = Vendor.search_count(domain)
            vendors = Vendor.search(domain, order='name, id', limit=limit, offset=offset)
            counts = _invoice_counts(vendors.ids, config)
            return json_response({'success': True, 'data': {
                'vendors': [v._freematica_settings_dict(counts) for v in vendors],
                'total': total,
                'filter_counts': {key: Vendor.search_count(base + dom) for key, dom in _VENDOR_FILTERS.items()},
                'tax_types': [{'code': code, 'label': label}
                              for code, label in Vendor._fields['freematica_tipo_impuesto'].selection],
            }})
        except Exception as error:
            _logger.error('Freematica vendors list error: %s', error)
            return _error(str(error))

    @http.route('/api/v1/freematica/vendors/<int:vendor_id>', type='http', auth='public',
                methods=['PATCH', 'OPTIONS'], csrf=False)
    def update_vendor(self, vendor_id, **kwargs):
        """PATCH {"default_accounting_account": "62900000" | null,
                  "freematica_tipo_impuesto": "S25" | null, "freematica_has_irpf": bool}
        Devuelve también los valores anteriores para poder deshacer."""
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()
        try:
            config = _config(kwargs)
            vendor = request.env['finia.ocr.vendor'].sudo().with_context(
                freematica_config_id=config.id).browse(vendor_id).exists()
            if not vendor:
                return _error('El proveedor no existe.')
            body = _body()
            vals = {}
            if 'default_accounting_account' in body:
                code = (body.get('default_accounting_account') or '').strip()
                if code:
                    if code.startswith('1'):
                        return _error('La cuenta "%s" es del grupo 1: no es una cuenta de gasto.' % code)
                    exists = request.env['freematica.account'].sudo()._freematica_scoped(config).search(
                        [('cod_cta', '=', code), ('cta_activa', '=', True), ('subcuenta', '=', True)], limit=1)
                    if not exists:
                        return _error('La cuenta "%s" no existe en el plan de cuentas de Freematica.' % code)
                vals['default_accounting_account'] = code or False
            if 'freematica_tipo_impuesto' in body:
                code = body.get('freematica_tipo_impuesto') or False
                valid = {c for c, _label in vendor._fields['freematica_tipo_impuesto'].selection}
                if code and code not in valid:
                    return _error('Tipo de impuesto no válido: %s' % code)
                vals['freematica_tipo_impuesto'] = code
            if 'freematica_has_irpf' in body:
                vals['freematica_has_irpf'] = bool(body.get('freematica_has_irpf'))
            if not vals:
                return _error('No hay nada que guardar.')
            previous = {key: vendor[key] for key in vals}
            vendor.write(vals)
            _logger.info('Proveedor %s actualizado desde Configuración: %s (antes %s)', vendor.id, vals, previous)
            return json_response({'success': True, 'data': {
                'vendor': vendor._freematica_settings_dict(_invoice_counts(vendor.ids, config)),
                'previous': {key: (value if value is not False else None) for key, value in previous.items()},
            }})
        except Exception as error:
            _logger.error('Freematica vendor update error (%s): %s', vendor_id, error)
            return _error(str(error))
