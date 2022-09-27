from ipaddress import IPv4Address

FASTLY_CNAME_TLS_1_2 = "d.sni.global.fastly.net"  # noqa: WPS114
FASTLY_CNAME_TLS_1_3 = "j.sni.global.fastly.net"  # noqa: WPS114
FASTLY_A_TLS_1_2 = [  # noqa: WPS114
    IPv4Address("151.101.2.133"),
    IPv4Address("151.101.66.133"),
    IPv4Address("151.101.130.133"),
    IPv4Address("151.101.194.133"),
]
FASTLY_A_TLS_1_3 = [  # noqa: WPS114
    IPv4Address("151.101.2.132"),
    IPv4Address("151.101.66.132"),
    IPv4Address("151.101.130.132"),
    IPv4Address("151.101.194.132"),
]
