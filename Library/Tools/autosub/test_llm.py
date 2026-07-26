import os
import sys
sys.path.append('/Users/shanfu/cc/Library/Tools/common')
from llm_utils import LLMClient, LLMProvider

client = LLMClient()
try:
    print("Testing glm-5.2 on dashscope...")
    res = client.generate_content("hello", model_name="glm-5.2", fallback=False, provider=LLMProvider.DASHSCOPE)
    print("Result:", res)
except Exception as e:
    print("Exception:", e)
