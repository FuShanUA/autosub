
import sys
import os
import argparse
import re

# Standard imports (Assuming srt_tool access for parsing if needed, but we can do local)
# For pure verbalizer, we just need text processing. But for SRT support we might use srt_tool helper.
SUBTOOL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'subtool')
sys.path.append(SUBTOOL_PATH)

try:
    import srt_tool
except ImportError:
    srt_tool = None


# --- Style Transfer Logic ---
def call_llm_style_transfer(text, style="Edgy/Intellectual", speaker=None):
    # Placeholder for LLM call
    context = ""
    if speaker:
        context = f" [Speaker: {speaker}]"
    return text # + f" (Verbalized [{style}]{context})"


# --- Text Processing ---
def is_editorial_content(line):
    line = line.strip()
    if not line: return False
    if line.startswith('[') and line.endswith(']'): return True
    if line.startswith('(') and line.endswith(')'): return True
    if line.lower().startswith("editor's note") or line.lower().startswith("note:"): return True
    return False

def extract_speaker(line):
    match = re.match(r'^([^:：]{2,20})[:：]\s*(.*)', line)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None, line

def process_text_file(input_path, output_path, style="Edgy/Intellectual"):
    print(f"Verbalizing Text: {input_path}")
    with open(input_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    verbalized_lines = []
    current_speaker = None
    
    for line in lines:
        stripped = line.strip()
        if is_editorial_content(stripped):
            verbalized_lines.append(line)
            continue
            
        speaker, content = extract_speaker(stripped)
        if speaker:
            current_speaker = speaker
            if content:
                new_content = call_llm_style_transfer(content, style, current_speaker)
                verbalized_lines.append(f"{speaker}: {new_content}\n")
            else:
                verbalized_lines.append(line)
        else:
            if stripped:
                new_content = call_llm_style_transfer(stripped, style, current_speaker)
                verbalized_lines.append(new_content + "\n")
            else:
                verbalized_lines.append(line)
                
    with open(output_path, 'w', encoding='utf-8') as f:
        f.writelines(verbalized_lines)
    print(f"Saved: {output_path}")

# --- SRT Processing ---
def process_srt_file(input_path, output_path, style="Edgy/Intellectual"):
    print(f"Verbalizing SRT: {input_path}")
    if not srt_tool:
        print("Error: srt_tool not found. Cannot parse SRT.")
        return

    blocks = srt_tool.parse_srt(input_path)
    verbalized_blocks = []
    
    for block in blocks:
        original_text = " ".join(block['lines'])
        # Simple processing, assume no speaker labels inside subtitles for now
        new_text = call_llm_style_transfer(original_text, style)
        
        new_block = block.copy()
        new_block['lines'] = [new_text]
        verbalized_blocks.append(new_block)
        
    srt_tool.write_srt(verbalized_blocks, output_path)
    print(f"Saved: {output_path}")

# --- Main Entry ---
def process_file(input_file, style="Edgy/Intellectual", output_file=None):
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        return

    # Auto-detect type
    # We DO NOT split/merge here. We just process the file given.
    # It is the job of the caller (Subtranslator) to pass the correct MONOLINGUAL file.
    
    base, ext = os.path.splitext(input_file)
    if not output_file:
         output_file = f"{base}.verbalized{ext}"

    is_srt = False
    if ext.lower() == '.srt':
        is_srt = True
    elif ext.lower() in ['.txt', '.md']:
        is_srt = False
    else:
        # peek
        try:
            with open(input_file, 'r', encoding='utf-8') as f:
                if '-->' in f.read(100): is_srt = True
        except:
            pass
            
    if is_srt:
        process_srt_file(input_file, output_file, style)
    else:
        process_text_file(input_file, output_file, style)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verbalizer: Pure Style Transfer")
    parser.add_argument('input_file', help="Path to input file (SRT/Text)")
    parser.add_argument('--style', default="Edgy/Intellectual", help="Style persona")
    parser.add_argument('--output', help="Optional output path")
    
    args = parser.parse_args()
    process_file(args.input_file, args.style, args.output)
