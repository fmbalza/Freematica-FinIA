# -*- coding: utf-8 -*-
import hashlib
import json
import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..services import freematica_client as client

_logger = logging.getLogger(__name__)


class FiniaBankConnection(models.Model):
    """Extiende la conexión bancaria con el código de cuenta de Freematica y el
    envío del Norma 43 generado por Finia (finia.bank.connection.export_n43)."""
    _inherit = 'finia.bank.connection'

    freematica_cod_banco = fields.Char(
        string='COD_BANCO Freematica', size=4,
        help='Código interno de la cuenta bancaria en Freematica (GET /pgrl/v2/bancos), '
             'no la entidad del banco. Es el `banco` que pide el importador de N43. Hay cuentas '
             'repetidas con distinto código (corriente y póliza): se elige a mano.',
    )
    freematica_n43_export_ids = fields.One2many(
        'freematica.n43.export', 'connection_id', string='Envíos de N43 a Freematica')

    def action_freematica_send_n43(self, date_from, date_to, force=False):
        """Genera el N43 del periodo y lo envía a Freematica. Un fichero idéntico
        a uno ya enviado no se reenvía salvo `force`."""
        self.ensure_one()
        if not self.freematica_cod_banco:
            raise UserError(_('Falta asignar el COD_BANCO de Freematica a esta cuenta bancaria.'))
        result = self.export_n43(date_from, date_to)
        file_hash = hashlib.sha256(result['content']).hexdigest()
        Export = self.env['freematica.n43.export'].sudo()
        previous = Export.search([
            ('connection_id', '=', self.id), ('file_hash', '=', file_hash), ('state', '=', 'enviado'),
        ], limit=1)
        if previous and not force:
            return {'sent': False, 'already_sent': True, 'export_id': previous.id, 'count': result['count']}

        config = self.env['freematica.config'].sudo().get_active_config()
        vals = {
            'connection_id': self.id,
            'date_from': fields.Date.to_date(date_from),
            'date_to': fields.Date.to_date(date_to),
            'cod_banco': self.freematica_cod_banco,
            'filename': result['filename'],
            'file_hash': file_hash,
            'movements_count': result['count'],
        }
        try:
            response = client.import_n43(
                config._as_client_config(), self.freematica_cod_banco, result['filename'],
                result['content'], token=config._ensure_valid_token(),
            )
        except client.FreematicaError as error:
            Export.create(dict(vals, state="error", response=error.mensaje))
            self.env.cr.commit()
            raise UserError(error.mensaje) from error
        export = Export.create(dict(vals, state='enviado', response=json.dumps(response, ensure_ascii=False)[:5000]))
        return {'sent': True, 'already_sent': False, 'export_id': export.id, 'count': result['count'],
                'response': response}
