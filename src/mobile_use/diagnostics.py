"""Bounded local exception evidence with input and payload redaction."""
import re


def exception_summary(exc, stage, sensitive_values=()):
    message = str(exc)
    for value in sorted({v for v in sensitive_values if v}, key=len, reverse=True):
        message = message.replace(value, '[redacted]')
    # Backend exceptions may include request arguments, URLs or XML. Keep the
    # first diagnostic line, not the serialized request/response that follows it.
    message = re.sub(r'(?is)<(?:hierarchy|node)\b.*', '[xml omitted]', message)
    message = re.sub(r'''(["'])(?:\\.|(?!\1).)*\1''', '[quoted value]', message)
    message = re.sub(r'https?://\S+', '[url]', message)
    message = re.sub(r'(?i)\bBearer\s+\S+', '[authorization omitted]', message)
    message = re.sub(r'(?im)\b(?:params|parameters|request|response|body|payload|authorization|cookie|'
                     r'password|passwd|token|secret|api[_-]?key)\s*[:=].*', '[payload omitted]', message)
    message = ' '.join(message.splitlines()[0].split()) if message else ''
    result = {'type': type(exc).__name__, 'stage': stage, 'message': message[:512]}
    if len(message) > 512:
        result['truncated'] = True
    if isinstance(getattr(exc, 'errno', None), int):
        result['errno'] = exc.errno
    return result
