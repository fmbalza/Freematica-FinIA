# -*- coding: utf-8 -*-
import logging

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import freematica_matching as matching
from .freematica_vendor_link import TAX_TYPES

_logger = logging.getLogger(__name__)

_MATCH_STATES = [
    ('no_intentado', 'No intentado'),
    ('coincidencia_nif', 'Coincidencia por NIF'),
    ('coincidencia_automatica', 'Coincidencia automática (nombre)'),
    ('coincidencia_manual', 'Coincidencia manual'),
    ('varios_nif', 'Varios proveedores con el mismo NIF'),
    ('sin_coincidencia', 'Sin coincidencia'),
]
# Estados que se vuelven a intentar automáticamente (al terminar el OCR, en el
# cron y al actualizar el catálogo). Una asignación manual nunca se toca.
_RETRY_STATES = ('no_intentado', 'sin_coincidencia', 'varios_nif')


class FiniaOcrVendor(models.Model):
    _name = 'finia.ocr.vendor'
    _inherit = 'finia.ocr.vendor'

    nif = fields.Char(
        string='NIF/CIF',
        help='NIF/CIF del proveedor. No lo extrae el OCR hoy; se completa a mano, o se '
             'autocompleta desde el proveedor Freematica matcheado si estaba vacío. Usado '
             'como BORRL_NIF al enviar facturas de este proveedor a Freematica.',
    )
    # ── Campos por empresa de Freematica ────────────────────────────────
    # Se guardan en freematica.vendor.link (uno por configuración) y aquí se
    # exponen los de la configuración en curso (contexto `freematica_config_id`;
    # sin él, la más antigua). Así el resto del código lee y escribe
    # `vendor.freematica_cod_aux` etc. sin saber de empresas: quien conoce la
    # empresa (factura, albarán, endpoint) fija el contexto.
    freematica_link_ids = fields.One2many('freematica.vendor.link', 'vendor_id', string='Vínculos Freematica')
    freematica_cod_aux = fields.Char(
        string='Código auxiliar Freematica', compute='_compute_freematica_link', inverse='_inverse_freematica_link',
        search='_search_freematica_cod_aux',
        help='Código auxiliar (BORRL_CODAUX) que identifica a este proveedor dentro de '
             'Freematica. Se autocompleta desde freematica_provider_id.cod_pro al '
             'matchear (por NIF o por nombre); si se edita a mano, esa edición manual '
             'nunca se pisa. Obligatorio para poder enviar sus facturas como asiento '
             'contable. Por empresa de Freematica.')
    freematica_provider_id = fields.Many2one(
        'freematica.provider', string='Proveedor Freematica',
        compute='_compute_freematica_link', inverse='_inverse_freematica_link')
    freematica_match_score = fields.Float(
        string='Score de coincidencia Freematica', compute='_compute_freematica_link',
        inverse='_inverse_freematica_link')
    freematica_match_state = fields.Selection(
        _MATCH_STATES, string='Estado coincidencia Freematica', compute='_compute_freematica_link',
        inverse='_inverse_freematica_link', search='_search_freematica_match_state')
    # Lo que había antes de la última asignación manual, para poder deshacerla
    # desde la propia ficha de la factura (restaurar intercambia ambos).
    freematica_prev_provider_id = fields.Many2one(
        'freematica.provider', string='Proveedor Freematica anterior',
        compute='_compute_freematica_link', inverse='_inverse_freematica_link')
    freematica_prev_match_state = fields.Selection(
        _MATCH_STATES, string='Estado coincidencia anterior', compute='_compute_freematica_link',
        inverse='_inverse_freematica_link')
    freematica_prev_cod_aux = fields.Char(
        string='Código auxiliar anterior', compute='_compute_freematica_link', inverse='_inverse_freematica_link')
    freematica_prev_default_account = fields.Char(
        string='Cuenta por defecto anterior', compute='_compute_freematica_link', inverse='_inverse_freematica_link')
    freematica_matched_at = fields.Datetime(
        string='Última resolución Freematica', compute='_compute_freematica_link', inverse='_inverse_freematica_link')
    freematica_tipo_impuesto = fields.Selection(
        TAX_TYPES, string='Tipo de impuesto Freematica', compute='_compute_freematica_link',
        inverse='_inverse_freematica_link',
        help='Tipo de impuesto (BORRID_TIMPUES) para las facturas de este proveedor cuando no '
             'es el habitual: intracomunitarias (S2/S21), inversión del sujeto pasivo (S15), '
             'importación (S3), no deducible (S1)... Vacío = automático: S0, S4 para bases '
             'al 0 % y S10 si la factura trae retención de IRPF. Tabla de Servinet, 2026-11.')
    freematica_has_irpf = fields.Boolean(
        string='Facturas con IRPF', compute='_compute_freematica_link', inverse='_inverse_freematica_link',
        help='Marcar si este proveedor es un profesional/autónomo sujeto a retención de '
             'IRPF (aplica a todas sus facturas, no varía factura a factura). Determina qué '
             'plantilla (BORR_PLANT) se usa al enviar sus facturas a Freematica: '
             'freematica.config.plantilla_irpf en vez de plantilla_normal. Finia no calcula '
             'ni extrae el importe de la retención hoy — esto solo decide la plantilla.')
    # Campo del módulo base: la cuenta de gasto por defecto es de cada empresa de
    # Freematica (cada una tiene su plan de cuentas), así que pasa a ser del vínculo.
    default_accounting_account = fields.Char(compute='_compute_freematica_link', inverse='_inverse_freematica_link',
                                             search='_search_freematica_default_account')

    _FREEMATICA_LINK_FIELDS = {
        'freematica_cod_aux': 'cod_aux',
        'freematica_provider_id': 'provider_id',
        'freematica_match_score': 'match_score',
        'freematica_match_state': 'match_state',
        'freematica_prev_provider_id': 'prev_provider_id',
        'freematica_prev_match_state': 'prev_match_state',
        'freematica_prev_cod_aux': 'prev_cod_aux',
        'freematica_prev_default_account': 'prev_default_account',
        'freematica_matched_at': 'matched_at',
        'freematica_tipo_impuesto': 'tipo_impuesto',
        'freematica_has_irpf': 'has_irpf',
        'default_accounting_account': 'default_accounting_account',
    }

    @api.depends_context('freematica_config_id')
    @api.depends('freematica_link_ids.cod_aux', 'freematica_link_ids.provider_id',
                 'freematica_link_ids.match_score', 'freematica_link_ids.match_state',
                 'freematica_link_ids.prev_provider_id', 'freematica_link_ids.prev_match_state',
                 'freematica_link_ids.prev_cod_aux', 'freematica_link_ids.prev_default_account',
                 'freematica_link_ids.matched_at', 'freematica_link_ids.tipo_impuesto',
                 'freematica_link_ids.has_irpf', 'freematica_link_ids.default_accounting_account')
    def _compute_freematica_link(self):
        config_id = self.env['freematica.config']._current().id
        links = {
            link.vendor_id.id: link for link in self.env['freematica.vendor.link'].sudo().search(
                [('vendor_id', 'in', self.ids), ('config_id', '=', config_id)])
        }
        for vendor in self:
            link = links.get(vendor.id)
            for field_name, link_field in self._FREEMATICA_LINK_FIELDS.items():
                if link:
                    vendor[field_name] = link[link_field]
                else:
                    vendor[field_name] = 'no_intentado' if link_field == 'match_state' else False

    def _inverse_freematica_link(self):
        config_id = self.env['freematica.config']._current().id
        Link = self.env['freematica.vendor.link'].sudo()
        for vendor in self:
            vals = {}
            for field_name, link_field in self._FREEMATICA_LINK_FIELDS.items():
                value = vendor[field_name]
                vals[link_field] = value.id if hasattr(value, '_name') else value
            link = Link.search([('vendor_id', '=', vendor.id), ('config_id', '=', config_id)], limit=1)
            if link:
                link.write(vals)
            elif any(value for key, value in vals.items() if key != 'match_state') \
                    or vals.get('match_state') not in (False, 'no_intentado'):
                Link.create(dict(vals, vendor_id=vendor.id, config_id=config_id))

    def _freematica_link_search(self, link_field, operator, value):
        config_id = self.env['freematica.config']._current().id
        Link = self.env['freematica.vendor.link'].sudo()
        base = [('config_id', '=', config_id)]
        empty = value is False or value is None or value == ''
        if operator in ('=', '!=') and empty:
            with_value = Link.search(base + [(link_field, '!=', False)]).mapped('vendor_id').ids
            return [('id', 'in' if operator == '!=' else 'not in', with_value)]
        links = Link.search(base + [(link_field, operator, value)])
        domain = [('id', 'in', links.mapped('vendor_id').ids)]
        if link_field == 'match_state':
            wanted = value if isinstance(value, (list, tuple)) else [value]
            if operator in ('=', 'in') and 'no_intentado' in wanted:
                # Sin vínculo = "no intentado".
                linked = Link.search(base).mapped('vendor_id').ids
                return ['|'] + domain + [('id', 'not in', linked)]
        return domain

    def _search_freematica_cod_aux(self, operator, value):
        return self._freematica_link_search('cod_aux', operator, value)

    def _search_freematica_match_state(self, operator, value):
        return self._freematica_link_search('match_state', operator, value)

    def _search_freematica_default_account(self, operator, value):
        return self._freematica_link_search('default_accounting_account', operator, value)

    def _freematica_resolve_provider(self, force=False):
        """Intenta matchear este proveedor Finia contra el catálogo cacheado
        de freematica.provider: primero por NIF exacto (alta confianza), y
        si no hay NIF o no matchea, por nombre con fuzzy-matching (mismo
        algoritmo que finia_facturaweb). Nunca pisa una coincidencia
        marcada como manual salvo que se pida force=True, ni un
        freematica_cod_aux/nif ya escritos a mano."""
        self.ensure_one()
        if self.freematica_match_state == 'coincidencia_manual' and self.freematica_provider_id and not force:
            return self.freematica_provider_id

        Provider = self.env['freematica.provider']
        provider = self.env['freematica.provider']
        match_state = 'sin_coincidencia'
        score = 0.0

        # NIF del proveedor: el escrito en este módulo (`nif`, a mano) o el que
        # lee el OCR (`vat`), descartando el NIF del propio cliente (el OCR a
        # veces lee el "CIF cliente" de la factura como si fuera del proveedor).
        normalized_nif = self._freematica_effective_nif()
        group = Provider._freematica_group_domain()
        same_nif = Provider.search([('normalized_nif', '=', normalized_nif)] + group) if normalized_nif else Provider
        if len(same_nif) == 1:
            provider = same_nif
            match_state = 'coincidencia_nif'
            score = 1.0
        elif len(same_nif) > 1:
            # Mismo NIF con varias fichas en Freematica (p. ej. una por
            # delegación): elegir una a ciegas mandaría las facturas al código
            # equivocado, así que se deja para que el usuario elija.
            match_state = 'varios_nif'
            score = 1.0

        # Por nombre SOLO si no hay NIF: si el proveedor trae NIF y no está en
        # Freematica, es que no está dado de alta (Segre Motors, 06/11/2026: se
        # emparejó por parecido con Soteras Motor y el asiento fue a otro
        # proveedor). Queda "Sin coincidencia" para darlo de alta o asignarlo.
        if not provider and match_state != 'varios_nif' and not normalized_nif:
            threshold = self.env['freematica.config']._current().provider_match_threshold
            candidates = [(p.id, p.nombre_pro) for p in Provider.search(group) if p.nombre_pro]
            best_id, best_score = matching.best_match(self.name, candidates, threshold=threshold)
            score = best_score
            if best_id:
                provider = Provider.browse(best_id)
                match_state = 'coincidencia_automatica'

        vals = {
            'freematica_match_score': score,
            'freematica_matched_at': fields.Datetime.now(),
            'freematica_match_state': match_state,
            'freematica_provider_id': provider.id if provider else False,
        }
        if match_state == 'varios_nif' and self.freematica_provider_id \
                and self.freematica_cod_aux == self.freematica_provider_id.cod_pro:
            # Venía de un emparejamiento automático anterior a esta regla.
            vals['freematica_cod_aux'] = False
        if provider:
            if not self.freematica_cod_aux:
                vals['freematica_cod_aux'] = provider.cod_pro
            # El NIF de la ficha nunca se copia: en un emparejamiento por nombre
            # sería el de OTRA empresa y fijaría el error para siempre.
            if not self.default_accounting_account and provider.cta_gasto:
                vals['default_accounting_account'] = provider.cta_gasto
        self.write(vals)
        return self.freematica_provider_id

    def _freematica_pending_invoices_domain(self, config=None):
        """Facturas que se van a enviar a Freematica: procesadas o
        contabilizadas, no enviadas y recibidas desde el inicio de envíos
        (de la empresa de la configuración)."""
        config = config or self.env['freematica.config']._current()
        domain = [('state', 'in', ('procesado', 'revisado', 'contabilizado', 'irregularidades')),
                  ('freematica_state', '!=', 'enviado')]
        if config.empresa_id:
            domain.append(('empresa_id', '=', config.empresa_id.id))
        if config.fecha_inicio_envio:
            since = fields.Datetime.to_datetime(config.fecha_inicio_envio)
            domain += ['|', ('reception_date', '>=', since),
                       '&', ('reception_date', '=', False), ('create_date', '>=', since)]
        return domain

    @api.model
    def _freematica_match_pending(self, retry_unmatched=True, config=None):
        """Empareja con el catálogo de Freematica los proveedores de las
        facturas pendientes de enviar que aún no lo están, para avisar ANTES
        de enviar (antes solo se intentaba al pulsar Enviar). Sin `config`
        recorre todas las configuraciones activas. Devuelve los proveedores que
        siguen sin coincidencia (de la última configuración procesada)."""
        if config is None:
            result = self.browse()
            for each in self.env['freematica.config'].sudo().search([('active', '=', True)], order='id'):
                result = self._freematica_match_pending(retry_unmatched, each)
            return result
        states = _RETRY_STATES if retry_unmatched else ('no_intentado',)
        invoices = self.env['finia.invoice'].sudo().search(self._freematica_pending_invoices_domain(config))
        vendors = invoices.mapped('ocr_vendor_id').with_context(freematica_config_id=config.id).filtered(
            lambda v: v.freematica_match_state in states and not v.freematica_cod_aux)
        for vendor in vendors:
            try:
                vendor._freematica_resolve_provider()
            except Exception as error:  # noqa: BLE001 - un proveedor raro no frena al resto
                _logger.warning('Freematica: no se pudo emparejar el proveedor %s: %s', vendor.id, error)
        return vendors.filtered(lambda v: not v.freematica_cod_aux)

    @api.model
    def _cron_freematica_match_pending(self):
        self._freematica_match_pending()

    @api.model
    def freematica_vendor_alerts(self, config=None):
        """Proveedores de facturas pendientes de enviar que no existen (o no
        se reconocen) en Freematica, con sus facturas: lo que muestra el panel
        de avisos del frontend."""
        config = config or self.env['freematica.config']._current()
        invoices = self.env['finia.invoice'].sudo().search(
            self._freematica_pending_invoices_domain(config) + [('ocr_vendor_id', '!=', False)],
            order='reception_date desc, id desc')
        alerts = []
        for vendor in invoices.mapped('ocr_vendor_id').with_context(freematica_config_id=config.id):
            if vendor.freematica_cod_aux:
                continue
            vendor_invoices = invoices.filtered(lambda i: i.ocr_vendor_id == vendor)
            alerts.append({
                'vendor_id': vendor.id,
                'vendor_name': vendor.name,
                'nif': vendor.nif or vendor.vat or None,
                'match_state': vendor.freematica_match_state,
                'candidates': [p._freematica_api_dict() for p in vendor._freematica_nif_candidates()],
                'previous_provider': vendor.freematica_prev_provider_id._freematica_api_dict() or None,
                'checked_at': vendor.freematica_matched_at and fields.Datetime.to_string(vendor.freematica_matched_at),
                'invoices': [{
                    'id': inv.id,
                    'name': inv.name,
                    'invoice_number': inv.ocr_invoice_number,
                    'invoice_date': inv.ocr_invoice_date and fields.Date.to_string(inv.ocr_invoice_date),
                    'total': inv.ocr_total_amount,
                    'state': inv.state,
                } for inv in vendor_invoices],
            })
        return alerts

    @api.model
    def _freematica_own_nifs(self):
        """NIF del propio cliente (no son de proveedores): parámetro
        `finia.own_vats` (separados por coma, el mismo que usa el OCR) y los
        NIF de los administradores dueños de cuenta."""
        param = self.env['ir.config_parameter'].sudo().get_param('finia.own_vats') or ''
        nifs = {matching.normalize_nif(v) for v in param.split(',')}
        owners = self.env['res.partner'].sudo().search([('finia_role', '=', 'admin'), ('vat', '!=', False)])
        nifs.update(matching.normalize_nif(p.vat) for p in owners)
        nifs.discard('')
        return nifs

    def _freematica_effective_nif(self):
        """NIF normalizado con el que buscar en Freematica ('' si no hay)."""
        self.ensure_one()
        own = self._freematica_own_nifs()
        for value in (self.nif, self.vat):
            nif = matching.normalize_nif(value)
            if nif and nif not in own:
                return nif
        return ''

    def _freematica_nif_candidates(self):
        """Fichas de Freematica con el NIF de este proveedor (más de una =
        hay que elegir)."""
        self.ensure_one()
        normalized_nif = self._freematica_effective_nif()
        if not normalized_nif:
            return self.env['freematica.provider']
        Provider = self.env['freematica.provider']
        return Provider.search([('normalized_nif', '=', normalized_nif)] + Provider._freematica_group_domain())

    def _freematica_snapshot_vals(self):
        """Valores actuales como "anteriores", para poder deshacer."""
        self.ensure_one()
        return {
            'freematica_prev_provider_id': self.freematica_provider_id.id,
            'freematica_prev_match_state': self.freematica_match_state,
            'freematica_prev_cod_aux': self.freematica_cod_aux,
            'freematica_prev_default_account': self.default_accounting_account,
        }

    def action_freematica_set_manual_match(self, provider_id):
        """Asigna a mano el proveedor de Freematica. Queda fijo (ningún cron lo
        cambia) y guarda lo anterior para poder restaurarlo."""
        self.ensure_one()
        provider = self.env['freematica.provider'].browse(provider_id).exists()
        if not provider:
            raise UserError(_('El proveedor de Freematica elegido ya no existe. Actualiza el catálogo.'))
        vals = self._freematica_snapshot_vals()
        vals.update({
            'freematica_provider_id': provider.id,
            'freematica_match_state': 'coincidencia_manual',
            'freematica_match_score': 1.0,
            'freematica_matched_at': fields.Datetime.now(),
            'freematica_cod_aux': provider.cod_pro,
        })
        if not self.default_accounting_account and provider.cta_gasto:
            vals['default_accounting_account'] = provider.cta_gasto
        self.write(vals)
        return True

    def action_freematica_restore_previous(self):
        """Deshace la última asignación manual: vuelve al proveedor anterior y
        el actual pasa a ser el "anterior" (así también se puede rehacer)."""
        self.ensure_one()
        if not self.freematica_prev_match_state:
            raise UserError(_('No hay una asignación anterior que restaurar.'))
        vals = self._freematica_snapshot_vals()
        vals.update({
            'freematica_provider_id': self.freematica_prev_provider_id.id,
            'freematica_match_state': self.freematica_prev_match_state,
            'freematica_cod_aux': self.freematica_prev_cod_aux,
            'default_accounting_account': self.freematica_prev_default_account,
            'freematica_matched_at': fields.Datetime.now(),
        })
        self.write(vals)
        return True

    def _freematica_needs_review(self):
        """Emparejado solo por nombre: conviene revisarlo."""
        self.ensure_one()
        return self.freematica_match_state == 'coincidencia_automatica'

    def _freematica_settings_dict(self, counts=None):
        """Fila de Configuración > Proveedores."""
        self.ensure_one()
        data = self._freematica_api_dict()
        tax_labels = dict(self._fields['freematica_tipo_impuesto'].selection)
        data.update({
            'ocr_nif': self.vat or None,
            'default_accounting_account': self.default_accounting_account or None,
            'tipo_impuesto': self.freematica_tipo_impuesto or None,
            'tipo_impuesto_label': tax_labels.get(self.freematica_tipo_impuesto) if self.freematica_tipo_impuesto else None,
            'has_irpf': bool(self.freematica_has_irpf),
            'needs_review': self._freematica_needs_review(),
            'pending_invoices': (counts or {}).get(('pending', self.id), 0),
            'sent_invoices': (counts or {}).get(('sent', self.id), 0),
        })
        return data

    def _freematica_api_dict(self):
        """Proveedor Finia y su ficha de Freematica, para el frontend."""
        self.ensure_one()
        return {
            'vendor_id': self.id,
            'vendor_name': self.name,
            'nif': self.nif or self.vat or None,
            'match_state': self.freematica_match_state,
            'match_score': self.freematica_match_score,
            'matched_at': self.freematica_matched_at and fields.Datetime.to_string(self.freematica_matched_at),
            'cod_aux': self.freematica_cod_aux or None,
            'provider': self.freematica_provider_id._freematica_api_dict() or None,
            'previous_provider': self.freematica_prev_provider_id._freematica_api_dict() or None,
            'previous_match_state': self.freematica_prev_match_state or None,
            'can_restore': bool(self.freematica_prev_match_state),
            'candidates': [p._freematica_api_dict() for p in self._freematica_nif_candidates()]
            if self.freematica_match_state == 'varios_nif' else [],
        }
