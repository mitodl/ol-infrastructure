from typing import Any, Dict, Text

production_defaults = {
    'rds': {
        'multi_az': True
    }
}

qa_defaults = {
    'rds': {
        'multi_az': False
    }
}


def defaults(stack_id: Text) -> Dict[Text, Any]:
    if stack_id.lower().endswith('qa'):
        return qa_defaults
    return production_defaults
