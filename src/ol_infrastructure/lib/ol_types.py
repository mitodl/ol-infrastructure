from enum import Enum


class BusinessUnit(str, Enum):  # noqa: WPS600
    bootcamps = 'bootcamps'
    micromasters = 'micromasters'
    xpro = 'mitxpro'
    ovs = 'odl-video'
    ocw = 'open-courseware'
    operations = 'operations'
    residential = 'residential'
    starteam = 'starteam'
    mit_open = 'mit-open'
