"""Default chart of accounts. (code, name, type, system_key, parent_code, contra)

Headers are the seven groups from the spec. Every key listed here is a stable handle the
posting code uses; users may rename accounts but cannot change code/type/key of these.
"""
A, L, E, R, C, O, X = ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "COGS", "OPEX", "OTHER_EXPENSE")

HEADERS = [
    ("1000", "Assets", A), ("2000", "Liabilities", L), ("3000", "Equity", E), ("4000", "Revenue", R),
    ("5000", "Cost of Goods Sold", C), ("6000", "Operating Expenses", O),
    ("7000", "Other Expenses and Finance Costs", X),
]

# (code, name, type, system_key, is_contra)
ACCOUNTS = [
    ("1010", "Cash on Hand", A, "CASH", False),
    ("1020", "Bank", A, "BANK", False),
    ("1030", "M-Pesa", A, "MPESA", False),
    ("1100", "Accounts Receivable", A, "AR", False),
    ("1200", "Inventory", A, "INVENTORY", False),
    ("1500", "Fixed Assets", A, "FIXED_ASSETS", False),
    ("1510", "Accumulated Depreciation", A, "ACCUM_DEPRECIATION", True),

    ("2010", "Accounts Payable", L, "AP", False),
    ("2020", "Outstanding Expenses", L, "ACCRUED_EXPENSES", False),
    ("2100", "Loans Payable", L, "LOANS", False),
    ("2200", "Tax Payable", L, "TAX_PAYABLE", False),
    ("2900", "Other Liabilities", L, "OTHER_LIABILITIES", False),

    ("3010", "Owner Capital", E, "OWNER_CAPITAL", False),
    ("3020", "Retained Earnings", E, "RETAINED_EARNINGS", False),
    ("3030", "Owner Drawings", E, "OWNER_DRAWINGS", True),
    ("3900", "Opening Balance Equity", E, "OPENING_EQUITY", False),

    ("4010", "Sales Revenue", R, "SALES_REVENUE", False),
    ("4020", "Sales Discounts", R, "SALES_DISCOUNTS", True),
    ("4030", "Sales Returns", R, "SALES_RETURNS", True),
    ("4110", "Rental Income", R, "INCOME_RENTAL", False),
    ("4120", "Interest Income", R, "INCOME_INTEREST", False),
    ("4130", "Service Income", R, "INCOME_SERVICE", False),
    ("4190", "Other Business Income", R, "INCOME_OTHER", False),

    ("5010", "Cost of Goods Sold", C, "COGS", False),
    ("5020", "Stock Losses and Adjustments", C, "STOCK_LOSS", False),
    ("5030", "Other Direct Costs", C, "DIRECT_COSTS", False),

    ("6010", "Rent", O, "EXP_RENT", False),
    ("6020", "Salaries and Wages", O, "EXP_SALARIES", False),
    ("6030", "Delivery and Transportation", O, "EXP_DELIVERY", False),
    ("6040", "Utilities", O, "EXP_UTILITIES", False),
    ("6050", "Marketing and Advertising", O, "EXP_MARKETING", False),
    ("6060", "Maintenance and Repairs", O, "EXP_MAINTENANCE", False),
    ("6070", "Communication", O, "EXP_COMMUNICATION", False),
    ("6080", "Security", O, "EXP_SECURITY", False),
    ("6090", "Licences and Permits", O, "EXP_LICENCES", False),
    ("6100", "Insurance", O, "EXP_INSURANCE", False),
    ("6110", "Banking and Transaction Charges", O, "EXP_BANKING", False),
    ("6120", "Packaging", O, "EXP_PACKAGING", False),
    ("6130", "Professional Services", O, "EXP_PROFESSIONAL", False),
    ("6140", "Depreciation", O, "EXP_DEPRECIATION", False),
    ("6150", "Taxes and Levies", O, "EXP_TAXES", False),
    ("6990", "Miscellaneous", O, "EXP_MISC", False),

    ("7010", "Loan Interest and Finance Costs", X, "EXP_FINANCE", False),
    ("7020", "Income Tax Expense", X, "TAX_EXPENSE", False),
]
