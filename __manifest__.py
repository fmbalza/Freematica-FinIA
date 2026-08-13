{
    'name': 'FinIA - Freematica',
    'version': '18.0.1.1.0',
    'category': 'Accounting',
    'summary': 'Integración con Freematica (e-Satellite): envío de facturas como asientos contables',
    'description': """
        FinIA - Freematica
        ===================

        Integra Finia con la API de Asientos Contables de Freematica
        (e-Satellite, `pcon/v2/import-asientos`) para enviar las facturas de
        proveedor contabilizadas en Finia como asientos de partida doble
        (Debe/Haber, IVA soportado, vencimientos).

        Sincroniza el catálogo de proveedores de Freematica
        (`pgrl/v2/proveedores`) y matchea automáticamente cada proveedor de
        Finia por NIF o por nombre, para resolver su código auxiliar y su
        cuenta contable real (varía por proveedor) sin mapeo manual.

        Sobreescribe el placeholder de finIA_backend
        (`finia.invoice.action_send_to_freematica` y el endpoint
        `/api/v1/invoices/<id>/send-to-freematica`) con la implementación real,
        sin modificar el módulo core.
    """,
    'author': 'Finia',
    'website': 'https://finia.app',
    'license': 'LGPL-3',
    'depends': ['base', 'finIA_backend'],
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/freematica_config_views.xml',
        'views/freematica_provider_views.xml',
        'views/finia_invoice_views.xml',
        'views/finia_ocr_vendor_views.xml',
        'views/menu.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
