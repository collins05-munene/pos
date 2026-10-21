from django.core.management.base import BaseCommand, CommandError

from assistant import conf, gemini


class Command(BaseCommand):
    help = "Check the Gemini setup: sends a tiny test prompt, or lists models your key can use."

    def add_arguments(self, parser):
        parser.add_argument("--list-models", action="store_true", help="List models available to your API key")

    def handle(self, *args, **opts):
        if not conf.api_key():
            raise CommandError("GEMINI_API_KEY is not set.")
        try:
            if opts["list_models"]:
                for name in gemini.list_models():
                    self.stdout.write(name)
                return
            self.stdout.write(f"Model: {conf.model()}")
            answer = gemini.generate(
                "Reply with the single word: ok",
                [{"role": "user", "parts": [{"text": "ping"}]}],
            )
        except gemini.GeminiError as exc:
            raise CommandError(f"{exc.user_message} (see the server log for details)")
        self.stdout.write(self.style.SUCCESS(f"Gemini replied: {answer!r}"))
