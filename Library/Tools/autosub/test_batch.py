import os
import sys
sys.path.append('/Users/shanfu/cc/Library/Tools/common')
from llm_utils import LLMClient, LLMProvider

client = LLMClient()
tasks = [
    {"prompt": f"Correct the misspellings in this text: block {i} is Trae Stephens", "chunk_idx": i}
    for i in range(6)
]

print("Starting batch test...")
try:
    res = client.generate_batch(tasks, model_name="glm-5.2", fallback=False, provider=LLMProvider.DASHSCOPE)
    print("Batch finished successfully with", len(res), "results")
    for r in res:
        print(f"Chunk {r['chunk_idx']} status:", "error" if "error" in r else "success")
except Exception as e:
    print("Batch failed:", e)
