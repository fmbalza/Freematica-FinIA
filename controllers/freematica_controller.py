# -*- coding: utf-8 -*-
import logging

from odoo import http
from odoo.http import request

from odoo.addons.finIA_backend.controllers.cors_utils import json_response, cors_preflight_response
from odoo.addons.finIA_backend.controllers.invoice_controller import InvoiceController as _BaseInvoiceController

_logger = logging.getLogger(__name__)


class InvoiceController(_BaseInvoiceController):
    """Mismo nombre de clase que el controller original a propósito: Odoo
    fusiona las rutas de controllers cross-módulo por nombre de clase (no
    por Python inheritance por sí sola) — si esta clase tuviera un nombre
    distinto, Odoo la trataría como un controller nuevo e independiente y
    volvería a registrar TODAS las rutas heredadas de InvoiceController
    (create_invoice, list_invoices, etc.), duplicándolas. Con el mismo
    nombre, solo `send_to_freematica` queda sobreescrito; el resto se
    resuelve una sola vez contra la clase base.

    Sobreescribe el placeholder de `InvoiceController.send_to_freematica`
    (finIA_backend/controllers/invoice_controller.py), que hoy siempre
    responde 501, para que devuelva el resultado real del envío. Se
    mantiene la misma ruta/método para que el frontend (que ya la llama)
    no necesite ningún cambio."""

    @http.route('/api/v1/invoices/<int:invoice_id>/send-to-freematica', type='http', auth='public',
                methods=['POST', 'OPTIONS'], csrf=False)
    def send_to_freematica(self, invoice_id, **kwargs):
        if request.httprequest.method == 'OPTIONS':
            return cors_preflight_response()

        # Nota: SIEMPRE se responde HTTP 200, incluso cuando success=False.
        # El cliente HTTP de Finia (api.service.ts) trata cualquier status
        # no-2xx como excepción y descarta el cuerpo de la respuesta sin
        # leerlo — si se devolviera 400/500 aquí, el frontend nunca vería
        # `error`/`error_codes` (necesarios para decidir si abrir el modal
        # de asignación de cuenta). Mismo patrón que ya usa
        # email_controller.py::connect_email_imap en finIA_backend.
        try:
            Invoice = request.env['finia.invoice'].sudo()
            invoice = Invoice.browse(invoice_id)
            if not invoice.exists():
                return json_response({'success': False, 'error': 'Invoice not found'}, 200)

            result = invoice._freematica_send_one()
            return json_response({
                'success': result.get('success', False),
                'message': result.get('message'),
                'error': result.get('error'),
                'data': {
                    'invoice_id': invoice.id,
                    'freematica_state': invoice.freematica_state,
                    'freematica_borr_cod': invoice.freematica_borr_cod,
                    'error_codes': result.get('error_codes') or [],
                },
            }, 200)
        except Exception as e:  # noqa: BLE001 - responder JSON en vez de tumbar el request
            _logger.error('Send to Freematica: Error for invoice %s: %s', invoice_id, str(e))
            return json_response({
                'success': False,
                'error': str(e),
            }, 200)
