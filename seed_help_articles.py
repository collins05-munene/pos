from django.core.management.base import BaseCommand

from support.models import HelpArticle

T, K = HelpArticle.Topic, HelpArticle.Kind

# Drafts only. They are created UNPUBLISHED because the steps must match your
# real screens. Edit them in the admin, then tick "Is published".
DRAFTS = [
    ("Getting started with your POS", "getting-started", T.GETTING_STARTED, K.GUIDE,
     "Set up your shop, add products and make your first sale.",
     "<ol><li>Add your categories, brands and units of measure.</li><li>Add products and their prices.</li>"
     "<li>Record opening stock.</li><li>Open the register and make a test sale.</li></ol>",
     "setup, begin, new shop, first sale"),
    ("How do I open and close the register?", "open-close-register", T.SALES, K.FAQ,
     "Start the day with a float and count the cash at closing.",
     "<ol><li>Go to Register, then Open Register and enter the opening float.</li>"
     "<li>At the end of the day go to Close Register and count the cash.</li></ol>",
     "till, cash up, float, shift"),
    ("Taking an M-PESA payment", "mpesa-payments", T.MPESA, K.GUIDE,
     "How customers pay by M-PESA and what to do if a payment doesn't show.",
     "<ol><li>Choose M-PESA at checkout.</li><li>Confirm the customer's phone number.</li>"
     "<li>Wait for the payment confirmation before handing over goods.</li></ol>",
     "mpesa, stk push, daraja, mobile money, paybill"),
    ("Receipt won't print", "receipt-not-printing", T.RECEIPTS, K.TROUBLESHOOTING,
     "Quick checks when the receipt printer stays silent or prints blank paper.",
     "<ol><li>Check the printer is on and has paper.</li><li>Check the cable or Bluetooth connection.</li>"
     "<li>Allow pop-ups for this site and try printing again.</li></ol>",
     "printer, thermal, blank receipt, print"),
    ("Receiving stock and adjusting stock levels", "stock-adjustments", T.STOCK, K.GUIDE,
     "Record deliveries, correct counts and see what is running low.",
     "<ol><li>Create a purchase order for the delivery.</li><li>Receive it to add to stock.</li>"
     "<li>Use Stock Management to adjust counts after a stock take.</li></ol>",
     "inventory, stock take, purchase order, low stock"),
    ("Reading your sales report", "sales-report", T.REPORTS, K.GUIDE,
     "Pick a date range and see sales, profit and payment methods.",
     "<ol><li>Open Reports, then Sales Report.</li><li>Choose a date range.</li>"
     "<li>Use the filters to narrow by product or cashier.</li></ol>",
     "report, profit, daily sales, export"),
]


class Command(BaseCommand):
    help = "Create starter help articles as unpublished drafts."

    def handle(self, *args, **opts):
        created = 0
        for title, slug, topic, kind, summary, body, keywords in DRAFTS:
            _, was_created = HelpArticle.objects.get_or_create(
                slug=slug,
                defaults=dict(title=title, topic=topic, kind=kind, summary=summary,
                              body=body, keywords=keywords, is_published=False),
            )
            created += was_created
        self.stdout.write(f"Created {created} draft article(s). Edit and publish them in the admin.")
