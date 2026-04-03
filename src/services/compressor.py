import logging

logger = logging.getLogger(__name__)


class ContextCompressor:
    def __init__(self) -> None:
        self.model = None
        try:
            from llmlingua import PromptCompressor  # type: ignore

            self.model = PromptCompressor(
                "microsoft/llmlingua-2-bert-base-multilingual-cased-selective-substitution",
                use_llmlingua2=True,
            )
            logger.info("LLMLingua-2 compressor initialized successfully.")
        except ImportError:
            logger.warning(
                "llmlingua not found. Falling back to simple extractive compression."
            )
        except Exception:
            self.model = None

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
