from typing import Any, Dict, Text

production_defaults = {
    'rds': {
        'multi_az': True,
        'instance_size': 'db.m6g.large'
    },
    'redis': {
        'instance_type': 'cache.m6g.large'
    }
}

qa_defaults = {
    'rds': {
        'instance_size': 'db.t3.medium',
        'multi_az': False,
        'prevent_delete': False,
        'take_final_snapshot': False,
    },
    'redis': {
        'instance_type': 'cache.t3.small'
    }
}


def defaults(stack_id: Text) -> Dict[Text, Any]:
    if stack_id.lower().endswith('qa'):
        return qa_defaults
    return production_defaults
