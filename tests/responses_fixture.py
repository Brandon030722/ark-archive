"""Independent native Responses fixtures for existing graph scenarios."""

def native_reply(chat):
    msg = chat['choices'][0]['message']
    usage = chat['usage']
    output = [{'type': 'reasoning', 'id': 'rs_fixture', 'summary': [],
               'encrypted_content': 'opaque-test-reasoning'}]
    for call in msg.get('tool_calls', []):
        output.append({'type': 'function_call', 'id': 'fc_' + call['id'],
                       'call_id': call['id'], 'status': 'completed', **call['function']})
    if msg.get('content'):
        output.append({'type': 'message', 'id': 'msg_fixture', 'role': 'assistant', 'status': 'completed',
                       'content': [{'type': 'output_text', 'text': msg['content'], 'annotations': []}]})
    return {'id': chat.get('id', 'resp_fixture'), 'model': chat['model'], 'status': 'completed',
            'output': output, 'usage': {'input_tokens': usage['prompt_tokens'],
            'output_tokens': usage['completion_tokens'], 'total_tokens': usage['total_tokens'],
            'input_tokens_details': {'cached_tokens': usage.get('prompt_tokens_details', {}).get('cached_tokens', 0)},
            'output_tokens_details': {'reasoning_tokens': 5}}}


def chat_view(native):
    messages = []
    for item in native['input']:
        kind = item.get('type')
        if kind == 'reasoning':
            continue
        if kind == 'function_call_output':
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': item['output']})
        elif kind == 'function_call':
            call = {'id': item['call_id'], 'type': 'function',
                    'function': {'name': item['name'], 'arguments': item['arguments']}}
            if messages and messages[-1]['role'] == 'assistant' and 'tool_calls' in messages[-1]:
                messages[-1]['tool_calls'].append(call)
            else:
                messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
        else:
            messages.append({'role': item['role'], 'content': item['content']})
    return {'model': native['model'], 'messages': messages,
            'tools': [{'type': 'function', 'function': {k: v for k, v in tool.items() if k not in {'type', 'strict'}}}
                      for tool in native.get('tools', [])]}
