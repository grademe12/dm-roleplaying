"""Model-facing read contract and request validation, shared by prompt and harness.

No inferred channel, argument repair, store mutation, or executable model input.
"""
from copy import deepcopy
import re

CHANNELS = ['slack', 'instagram']
LIMIT = {'type': 'integer', 'minimum': 1, 'maximum': 10}
OFFSET = {'type': 'integer', 'minimum': 0, 'default': 0}
ORDER = {'type': 'string', 'enum': ['newest', 'oldest'], 'default': 'newest'}
BEFORE = {'type': ['integer', 'null'], 'minimum': 0, 'default': None}
KEY = {'type': ['string', 'null'], 'minLength': 1, 'maxLength': 80, 'pattern': r'[^\s/\\]+'}
SCOPES = {'type': 'array', 'minItems': 1, 'maxItems': 6, 'uniqueItems': True,
          'items': {'type': 'string', 'minLength': 1, 'maxLength': 80, 'pattern': r'[^\s/\\]+'},
          'default_from': 'memory_policy.scopes (omit this argument to use configured scopes)'}


def keywords(maximum, minimum=1, max_length=None):
    item = {'type': 'string', 'minLength': 1}
    if max_length is not None: item['maxLength'] = max_length
    return {'type': 'array', 'minItems': minimum, 'maxItems': maximum, 'items': item}


def schema(required, properties, example, **extra):
    return {'type': 'object', 'required': required, 'properties': properties,
            'additionalProperties': False, 'example': example, **extra}


SCHEMAS = {
    'recall': schema(['keywords'], {'keywords': keywords(20, minimum=0), 'limit': {**LIMIT, 'default': 10}},
        {'operation': 'recall', 'arguments': {'keywords': ['설명']}}),
    'memory-recall': schema(['keywords'], {'keywords': keywords(10, max_length=100),
        'limit': {**LIMIT, 'default': 10}, 'offset': OFFSET, 'order': ORDER, 'before_ms': BEFORE},
        {'operation': 'memory-recall', 'arguments': {'keywords': ['설명'], 'order': 'oldest'}}),
    'memory-context': schema(['channel'], {
        'channel': {'type': 'string', 'enum': CHANNELS}, 'selected_scopes': SCOPES, 'key': KEY,
        'history': {'type': 'boolean', 'default': False}, 'offset': OFFSET, 'limit': {**LIMIT, 'default': 6}},
        {'operation': 'memory-context', 'arguments': {'channel': 'slack', 'key': 'returned-key', 'history': True}},
        conditional_requirements=[{'when': {'history': True}, 'required_non_null': ['key']}]),
    'memory-history-search': schema(['channel', 'keywords'], {
        'channel': {'type': 'string', 'enum': CHANNELS}, 'keywords': keywords(10, max_length=100),
        'selected_scopes': SCOPES, 'offset': OFFSET, 'limit': {**LIMIT, 'default': 6},
        'order': ORDER, 'before_ms': BEFORE},
        {'operation': 'memory-history-search', 'arguments': {'channel': 'slack', 'keywords': ['애칭']}}),
}


class QueryContractError(ValueError):
    def __init__(self, code, details):
        super().__init__(code)
        self.code, self.details = code, details


def contract():
    return {'version': 2, 'operations': list(SCHEMAS),
        'request_shape': {'operation': 'one supported operation', 'arguments': 'JSON object'},
        'session': 'main (fixed; do not include a session argument)', 'limit_max': 10,
        'operation_schemas': deepcopy(SCHEMAS), 'history_requires_key': True,
        'channel_guidance': 'memory-context와 memory-history-search는 channel이 필수이며 기본값이 없다. '
            '허용값은 slack/instagram이다. 질문에 채널이 명시되면 그 채널을 조회한다. '
            '현재 대화 채널이 입력으로 확인되면 그 채널부터 조회할 수 있다. '
            '채널을 모르면 한 채널을 답으로 추정하지 말고 두 채널을 각각 명시해 확인한다. '
            'all 같은 합쳐진 채널은 없다.',
        'history_guidance': 'recall/memory-recall과 기본 memory-context는 활성 기억만 반환한다. '
            '과거·변경·철회 질문이면 key를 알 때 memory-context(history=true)로 근거를 읽는다. '
            'key를 모르면 memory-history-search의 실제 반환 key를 사용한다. '
            '예시의 returned-key는 자리표시자이며 실제 key가 아니다. '
            '발견 메타데이터를 현재 허용이나 발언 원문 근거로 대신 사용하지 않는다.',
        'error_guidance': 'ok=false이면 오류이며 무매칭이 아니다. error_details의 누락/잘못된 필드를 '
            '수정해 새 조회를 요청한다. 하니스는 누락 필드를 채우거나 자동 재시도하지 않는다.'}


def invalid(value, rule):
    types = rule['type'] if isinstance(rule['type'], list) else [rule['type']]
    actual = ('null' if value is None else 'boolean' if type(value) is bool else
              'integer' if type(value) is int else 'string' if isinstance(value, str) else
              'array' if isinstance(value, list) else 'other')
    if actual not in types: return 'wrong_type'
    if value is None: return None
    if 'enum' in rule and value not in rule['enum']: return 'unsupported_value'
    if actual == 'integer':
        if value < rule.get('minimum', value) or value > rule.get('maximum', value): return 'out_of_range'
    if actual == 'string':
        if not value.strip() or len(value) < rule.get('minLength', 0): return 'empty_string'
        if len(value) > rule.get('maxLength', len(value)): return 'too_long'
        if 'pattern' in rule and re.fullmatch(rule['pattern'], value) is None: return 'invalid_token'
    if actual == 'array':
        if not rule.get('minItems', 0) <= len(value) <= rule.get('maxItems', len(value)): return 'invalid_count'
        if any(invalid(item, rule['items']) for item in value): return 'invalid_item'
        if rule.get('uniqueItems') and len(value) != len(set(value)): return 'duplicate_item'
    return None


def validate(request):
    if not isinstance(request, dict) or set(request) != {'operation', 'arguments'}:
        raise QueryContractError('invalid_query_request', {
            'required_fields': ['operation', 'arguments'], 'additional_fields_allowed': False})
    operation, args = request['operation'], request['arguments']
    if not isinstance(operation, str) or operation not in SCHEMAS:
        raise QueryContractError('unsupported_query_operation', {'allowed_operations': list(SCHEMAS)})
    specification = SCHEMAS[operation]
    if not isinstance(args, dict):
        raise QueryContractError('invalid_query_arguments', {'operation': operation, 'expected_type': 'object'})
    missing = set(specification['required']) - set(args)
    for condition in specification.get('conditional_requirements', []):
        if all(args.get(k) is v for k, v in condition['when'].items()):
            missing.update(k for k in condition['required_non_null'] if args.get(k) is None)
    unknown = set(args) - set(specification['properties'])
    failures = []
    for field, value in args.items():
        if field in specification['properties']:
            rule = specification['properties'][field]
            reason = invalid(value, rule)
            if reason: failures.append({'field': field, 'reason': reason, 'expected': deepcopy(rule)})
    if missing or unknown or failures:
        raise QueryContractError('invalid_query_arguments', {
            'operation': operation, 'missing_fields': sorted(missing), 'unknown_fields': sorted(unknown),
            'invalid_fields': failures, 'allowed_values': {'channel': list(CHANNELS)} if 'channel' in specification['properties'] else {},
            'example': deepcopy(specification['example']),
            'instruction': '필드를 수정해 새 요청을 작성한다. 기본 채널 대입이나 자동 재시도는 하지 않는다.'})
    return operation, args
