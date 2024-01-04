def build_tags_document(source_tags: dict[str, str]):
    tag_list = []
    for key, value in source_tags.items():
        tag_list.append({"Key": key, "Value": value})
    return {
        "DryRun": False,
        "Resources": [],
        "Tags": tag_list,
    }
