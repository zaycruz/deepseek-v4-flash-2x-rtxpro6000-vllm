FROM vllm/vllm-openai:v0.25.1

RUN python3 -m pip install --no-cache-dir --upgrade "flashinfer-python==0.6.14"

LABEL org.opencontainers.image.title="DeepSeek V4 Flash vLLM SM120 DSpark" \
      org.opencontainers.image.description="Pinned vLLM 0.25.1 and FlashInfer 0.6.14 runtime for dual RTX PRO 6000" \
      org.opencontainers.image.licenses="Apache-2.0"
