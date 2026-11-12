# -*- coding: utf-8 -*-
from odoo import models
from odoo.exceptions import UserError


class FiniaInvoiceLine(models.Model):
    _inherit = 'finia.invoice.line'

    def _resolve_default_accounting_account(self, invoice, category):
        """La cuenta de gasto por defecto del proveedor es de cada empresa de
        Freematica: se resuelve con la configuración de la empresa de la factura."""
        try:
            config = self.env['freematica.config'].sudo().get_config_for(invoice.empresa_id)
        except UserError:
            return super()._resolve_default_accounting_account(invoice, category)
        scoped = self.with_context(freematica_config_id=config.id)
        return super(FiniaInvoiceLine, scoped)._resolve_default_accounting_account(
            invoice.with_context(freematica_config_id=config.id), category)
