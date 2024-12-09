<%
    import json
    import os

    enabled = middleware.call_sync('network.configuration.config')['service_announcement']['wsd']
    smb_config = middleware.call_sync('smb.config')

    # We use bindip_choices because this is cluster-aware and will give ctdb public IPs
    interfaces = smb_config['bindip'] or list(middleware.call_sync('smb.bindip_choices').values())

    conf = {
        'realm': middleware.call_sync('smb.getparm', 'realm', 'GLOBAL'),
        'netbios_name': smb_config['netbiosname'],
        'workgroup': smb_config['workgroup'],
        'interfaces': interfaces,
        'enabled': middleware.call_sync('network.configuration.config')['service_announcement']['wsd']
    }

    try:
        os.chown('/var/log/wsdd.log', 1, 1)
    except FileNotFoundError:
        with open('/var/log/wsdd.log', 'w') as f:
            os.fchown(f.fileno(), 1, 1)

%>
${json.dumps(conf)}
