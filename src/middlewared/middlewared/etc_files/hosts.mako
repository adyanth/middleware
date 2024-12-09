<%
        from middlewared.plugins.network_.global_config import HOSTS_FILE_EARMARKER

	network_config = middleware.call_sync('network.configuration.config')
	ad_config = middleware.call_sync('datastore.config', 'directoryservice.activedirectory')
	hostname = network_config['hostname_local']
	domain_name = network_config['domain']
	if ad_config['ad_enable']:
		hostname = middleware.call_sync('smb.config')['netbiosname'].lower()
		domain_name = ad_config['ad_domainname'].lower()
%>
127.0.0.1	${hostname}.${domain_name} ${hostname}
127.0.0.1	localhost

# The following lines are desirable for IPv6 capable hosts
::1	localhost ip6-localhost ip6-loopback
ff02::1 ip6-allnodes
ff02::2 ip6-allrouters

${HOSTS_FILE_EARMARKER}
% for host in network_config['hosts']:
${host}
% endfor
