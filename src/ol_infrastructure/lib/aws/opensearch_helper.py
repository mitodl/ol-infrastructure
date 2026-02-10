from enum import StrEnum, unique


@unique
class SearchInstanceTypes(StrEnum):
    small = "t3.small.search"
    medium = "t3.medium.search"
    general_purpose_large = "m7g.large.search"
    general_purpose_xlarge = "m7g.xlarge.search"
    high_mem_regular = "r7g.large.search"
    high_mem_xlarge = "r7g.2xlarge.search"
