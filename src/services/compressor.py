import logging

logger = logging.getLogger(__name__)


class ContextCompressor:
    # LLMLingua-2 다국어 모델 후보(우선순위). 첫 모델이 HF 401/오프라인 등으로 로드
    # 실패하면 다음 후보로 폴백하고, 모두 실패하면 단순추출(_simple_compress)을 쓴다.
    # selective-substitution 이 품질상 우선이나 접근 불가 환경에선 meetingbank 가 받친다.
    _MODEL_CANDIDATES = (
        "microsoft/llmlingua-2-bert-base-multilingual-cased-selective-substitution",
        "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank",
    )

    def __init__(self) -> None:
        self.model = None
        try:
            from llmlingua import PromptCompressor  # type: ignore
        except ImportError:
            logger.warning(
                "llmlingua not found. Falling back to simple extractive compression."
            )
            return

        for model_name in self._MODEL_CANDIDATES:
            try:
                self.model = PromptCompressor(model_name, use_llmlingua2=True)
                logger.info("LLMLingua-2 compressor initialized: %s", model_name)
                return
            except Exception as e:  # noqa: BLE001 - 후보 폴백을 위해 광범위 포착
                logger.warning("LLMLingua-2 model '%s' load failed: %s", model_name, e)
                self.model = None
        logger.warning(
            "All LLMLingua-2 models failed. Using simple extractive compression."
        )

    def compress(
        self, context: str, instruction: str = "", target_token: int = 2000
    ) -> str:
        if not context or len(context) < 500:
            return context

        if self.model:
            try:
                result = self.model.compress_prompt(
                    context,
                    instruction=instruction,
                    target_token=target_token,
                    rank_method="longllmlingua",
                )
                res: str = result.get("compressed_prompt", context)
                return res
            except Exception as e:
                logger.error(f"LLMLingua compression failed: {e}")
                return self._simple_compress(context, target_token)
        else:
            return self._simple_compress(context, target_token)

    def _simple_compress(self, text: str, target_token: int) -> str:
        max_chars = target_token * 4
        if len(text) <= max_chars:
            return text

        logger.debug(f"Simple compressing text from {len(text)} to ~{max_chars} chars.")

        half_limit = max_chars // 2
        start_chunk = text[:half_limit]
        end_chunk = text[-half_limit:]

        return f"{start_chunk}\n\n[... (content compressed for efficiency) ...]\n\n{end_chunk}"
