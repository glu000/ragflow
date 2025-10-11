with open('./chat_model.py', 'r') as f:
    lines = f.readlines()

new_lines = []
for i, line in enumerate(lines):
    new_lines.append(line)
    
    # Fix 1: Add _clean_conf call after system line in GoogleChat._chat
    if 'class GoogleChat(Base):' in line:
        in_google_chat = True
    elif line.strip().startswith('class ') and 'GoogleChat' not in line:
        in_google_chat = False
    
    if 'in_google_chat' in locals() and in_google_chat and '    def _chat(self, history, gen_conf={}, **kwargs):' in line:
        if i + 1 < len(lines) and 'system = ' in lines[i + 1]:
            new_lines.append(lines[i + 1])
            new_lines.append('        gen_conf = self._clean_conf(gen_conf)\n')
            lines[i + 1] = ''

# Fix 2: Delete max_tokens after conversion in _clean_conf
fixed_content = ''.join(new_lines)
fixed_content = fixed_content.replace(
    '                gen_conf["max_output_tokens"] = gen_conf["max_tokens"]\n',
    '                gen_conf["max_output_tokens"] = gen_conf["max_tokens"]\n                del gen_conf["max_tokens"]\n'
)

with open('./chat_model.py', 'w') as f:
    f.write(fixed_content)

print("✓ Fix applied to chat_model.py")
