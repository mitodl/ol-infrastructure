import json

from django.conf import settings


def dump_settings_to_json():
    """
    Captures all Django settings and writes them to /tmp/settings.json.
    Non-serializable values are converted to strings.
    """
    settings_dict = {}
    # Iterate over settings attributes, selecting only uppercase ones (the convention for settings).
    for attr in dir(settings):
        if attr.isupper():
            value = getattr(settings, attr)
            # Ensure the value is JSON serializable, converting to string if not.
            try:
                json.dumps(value)
                settings_dict[attr] = value
            except TypeError:
                settings_dict[attr] = str(value)
    file_path = "/tmp/settings.json"
    try:
        with open(file_path, "w") as f:
            json.dump(settings_dict, f, indent=4)
        print(f"Successfully wrote settings to {file_path}")
    except Exception as e:
        print(f"Error writing to file: {e}")


# --- To use, simply call the function after pasting it in the shell ---
dump_settings_to_json()
