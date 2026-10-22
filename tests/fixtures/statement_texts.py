"""SYNTHETIC retirement-plan statement text (no real person, plan or number). Each constant is a list of page strings.
EMPOWER_STYLE: a 401(k) with a collective trust (no ticker), a stable value fund, a target-date fund, company stock, and the personal details a real
statement carries (so redaction has something to remove)."""

EMPOWER_STYLE = [
    # page 1: cover
    """Empower Retirement
Quarterly Statement
Statement Period: January 1, 2026 - March 31, 2026
Plan Name: Acme Corporation 401(k) Retirement Savings Plan
Prepared for
Jordan Q Sample
1428 Maple Grove Lane, Apt 4B
Springfield, IL 62704
Phone: (217) 555-0142   Email: jordan.sample@example.com
Participant ID: 8841-2207-5531
SSN: ***-**-6789   Date of Birth: 07/14/1984
Account Number: 123456789012
Your Account Balance at a glance
Beginning balance $118,402.55
Contributions $4,950.00
Investment gain/loss $3,211.40
Ending balance $126,563.95""",
    # page 2: summary
    """Account Summary
Source                  Beginning     Ending
Pre-tax                 $90,100.10    $96,422.80
Roth                    $18,302.45    $19,141.15
Employer match          $10,000.00    $11,000.00
Total                   $118,402.55   $126,563.95
Vested balance $124,000.00""",
    # page 3: balance by investment
    """Balance by Investment
Investment Option                         Shares/Units    Unit Value    Ending Balance   % of Total
Acme Stable Value Fund                    3,120.4410      $14.2310      $44,405.62       35.1%
Acme S&P 500 Index CIT                    190.2210        $210.3300     $40,010.74       31.6%
Acme Target Retirement 2050 Fund          812.5000        $21.0100      $17,070.63       13.5%
Acme Corporation Common Stock (ACME)      104.0000        $87.4000      $9,089.60        7.2%
Acme International Equity CIT             240.0000        $65.7200      $15,772.80       12.5%
Total                                                                   $126,349.39""",
    # page 4: continuation of the table (no keywords, only rows)
    """Balance by Investment (continued)
Acme Cash Reserve                         214.5600        $1.0000       $214.56          0.2%
Total ending balance                                                    $126,563.95
Percent of total 100.0%
12.1  8.2  4.4  3.3  2,411.12  1,031.55  887.21  54.20""",
    # page 5: activity
    """Transaction Activity
Date          Description                 Amount
01/15/2026    Employee contribution       $1,650.00
02/15/2026    Employee contribution       $1,650.00
03/15/2026    Employee contribution       $1,650.00""",
    # page 6: disclosures
    """Important Disclosures
This statement is provided for information only. Past performance is not a guarantee of future results.
For more information about your plan, see the privacy policy and terms and conditions on our website.
Glossary of terms: Unit value is the price of one unit of a collective investment trust.""",
]


# Other recordkeepers' layouts (all synthetic). Different words for the same things: "Quantity/Price/Value", "units ... money type", "Plan:" labels.
FIDELITY_STYLE = [
    """Fidelity NetBenefits
Your Globex Corp 401(k) Plan statement
Quarter ending March 31, 2026
Plan: Globex Corp 401(k) Plan
Account number 998877665  Phone 800-555-0100
Total account value $52,100.10
Beginning value $48,000.00""",
    """Your investments
Investment                                  Quantity     Price      Value
FXAIX  Fidelity 500 Index Fund              120.553      $190.40    $22,953.26
FSPSX  Fidelity International Index Fund    210.100      $50.12     $10,530.41
Managed Income Portfolio                    8,900.220    $1.0000    $8,900.22
Globex Corp Stock Fund (GLBX)               80.000       $116.00    $9,280.00
Fidelity Government Cash Reserves           436.200      $1.0000    $436.20
Total                                                               $52,100.09""",
    """Important information
This statement is for information only. Past performance is not a guarantee of future results. See the privacy policy.""",
]

PRINCIPAL_STYLE = [
    """Principal Financial Group
Retirement account statement  01/01/2026 - 03/31/2026
Plan name: Initech Retirement Savings Plan 401(k)
Participant: Pat Lee
Account balance as of 03/31/2026: $9,051.07""",
    """Account balance by investment and money type
Principal LifeTime 2045 Separate Account
   Salary deferral        120.5500 units    $31.2200     $3,763.55
   Roth deferral           40.0000 units    $31.2200     $1,248.80
   Safe harbor match       30.1000 units    $31.2200     $939.76
Initech Stable Value Option
   Salary deferral                                       $2,000.00
   Safe harbor match                                     $1,099.00
Total balance                                            $9,051.11""",
]

VANGUARD_403B_STYLE = [
    """Vanguard
Springfield School District 403(b) Plan
Statement period: 01/01/2026 to 03/31/2026
Ending account balance $30,880.00""",
    """Balance by investment
Fund                                          Shares      Share price    Balance
Target Retirement 2040 Fund (VFORX)           300.000     $42.10         $12,630.00
Total Stock Market Index Fund (VTSAX)         150.000     $121.00        $18,150.00
Plan Cash Reserve                             100.000     $1.0000        $100.00
Total                                                                    $30,880.00""",
]
