import os, json, html, shutil
from PIL import Image
# ---- CONFIG: edit, then run `python3 build.py` ----
DOMAIN="https://unisalesystems.store"
LOGIN="https://app.unisalesystems.store/users/login/"
REGISTER="https://app.unisalesystems.store/signup/"
FORM_ACTION=""   # your form endpoint (Formspree etc). Until set, the form tells visitors it is not connected.
SOCIALS={}       # {"LinkedIn":"https://..."} shown in the footer only if filled
GA_ID=""
LEGAL={"entity":"[Legal entity name - to be confirmed]","law":"[Governing law - to be confirmed]","date":"[date - to be confirmed]"}
NAV=[("/","Home"),("/about/","About Us"),("/our-story/","Our Story"),("/products/","Products"),("/contact/","Contact")]
A="/assets/"
def btn(h,t,c="go"): return f'<a class="btn {c}" href="{h}">{t}</a>'
def sec(i,c=""): return f'<section class="sec {c}"><div class="wrap">{i}</div></section>'
def paras(t): return "".join(f"<p>{x}</p>" for x in t.strip().split("\n\n"))
def head(h,p=""): return f'<section class="page"><div class="wrap"><h1>{h}</h1><p class="lead">{p}</p></div></section>'
def exists(p): return os.path.exists("assets/"+p)
def img(p,alt):
    w,h=Image.open("assets/"+p).size
    return f'<figure class="shot"><img src="{A}{p}" alt="{alt}" width="{w}" height="{h}" loading="lazy"></figure>'
SH=[("dashboard","Unisale POS sales dashboard"),("sales","Unisale POS sales terminal"),("inventory","Unisale POS inventory and stock levels"),("reports","Unisale POS sales report"),("expenses","Unisale POS cash-out and expenses")]
SHOTS=[img(f"products/unisale-pos/{k}.png",a) for k,a in SH if exists(f"products/unisale-pos/{k}.png")]
def gallery(): return '<div class="gallery">'+"".join(SHOTS)+"</div>" if SHOTS else ""
def founder(body,cta=True):
    ph=exists("founder/collins-gitonga.jpg")
    pic=f'<figure style="margin:0"><img src="{A}founder/collins-gitonga.jpg" alt="Collins Gitonga, founder of Unisale Systems" width="340" height="340" loading="lazy"></figure>' if ph else ""
    return f'<div class="founder{"" if ph else " solo"}">{pic}<div>{body}</div></div>'
HI=["Sales terminal with receipts and refunds","Stock levels, adjustments and purchase orders","Suppliers, categories and brands","Cash-outs and expenses from the till","Sales dashboard and PDF reports","Multiple branches with stock transfers"]
def hl(): return '<ul class="tick" style="color:var(--ink)">'+"".join(f"<li>{x}</li>" for x in HI)+"</ul>"
def pricing(dark=True): return f'''<section class="sec ink"><div class="wrap two"><div><h2>Simple pricing.</h2><p>No hidden tiers. Start free for a month and pay only for what you use.</p><div class="row">{btn(REGISTER,"Get Started")}{btn("/contact/","Ask a question","onink")}</div></div>
<div><div class="cost"><div><b>KES 3,000</b><span>one-time installation fee</span></div><div><b>1st month free</b><span>then KES 1,499 a month</span></div><div><b>KES 100</b><span>per extra branch, per month</span></div></div><p>The monthly fee covers 2 branches.</p></div></div></section>'''
ORG={"@type":"Organization","name":"Unisale Systems","url":DOMAIN+"/","logo":DOMAIN+A+"brand/unisale-systems-logo.png"}
def page(path,title,desc,body):
    nav="".join(f'<li><a href="{h}"{" aria-current=page" if h==path else ""}>{t}</a></li>' for h,t in NAV)
    soc="".join(f'<a href="{u}" rel="noopener me">{n}</a> ' for n,u in SOCIALS.items())
    ga=f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script><script>window.dataLayer=[];function gtag(){{dataLayer.push(arguments)}}gtag("js",new Date());gtag("config","{GA_ID}")</script>' if GA_ID else ""
    t=html.escape(title); d=html.escape(desc); og=DOMAIN+A+"brand/og-image.png"
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{t}</title><meta name="description" content="{d}"><link rel="canonical" href="{DOMAIN+path}"><meta name="theme-color" content="#0F2350">
<meta property="og:title" content="{t}"><meta property="og:description" content="{d}"><meta property="og:url" content="{DOMAIN+path}"><meta property="og:image" content="{og}"><meta property="og:type" content="website"><meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="{A}icons/favicon.png" type="image/png"><link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700;12..96,800&family=Figtree:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;700&display=swap"><link rel="stylesheet" href="/css/style.css">
<script type="application/ld+json">{json.dumps({"@context":"https://schema.org","@graph":[ORG]})}</script>{ga}</head><body><a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap bar"><a class="brand" href="/" aria-label="Unisale Systems home"><img src="{A}brand/unisale-systems-logo.png" alt="" width="30" height="30"><span>Unisale Systems</span></a>
<button class="burger" aria-expanded="false" aria-controls="menu" aria-label="Menu"><span></span><span></span></button><div class="menu" id="menu"><ul>{nav}</ul><div class="acts">{btn(LOGIN,"Login","line")}{btn(REGISTER,"Get Started")}</div></div></div></header>
<main id="main">{body}</main>
<footer class="foot"><div class="wrap fgrid"><div><a class="brand" href="/"><img src="{A}brand/unisale-systems-logo-light.png" alt="" width="30" height="30"><span>Unisale Systems</span></a><p>Practical digital products that simplify processes and help businesses operate smarter.</p>{soc}</div>
<div><h2>Company</h2><ul><li><a href="/about/">About Us</a></li><li><a href="/our-story/">Our Story</a></li><li><a href="/contact/">Contact</a></li></ul></div><div><h2>Products</h2><ul><li><a href="/products/">All products</a></li><li><a href="/unisale-pos/">Unisale POS</a></li><li><a href="{LOGIN}">Login</a></li><li><a href="{REGISTER}">Get Started</a></li></ul></div><div><h2>Legal</h2><ul><li><a href="/privacy-policy/">Privacy Policy</a></li><li><a href="/terms/">Terms and Conditions</a></li></ul></div></div>
<div class="wrap copy">&copy; 2026 Unisale Systems. All rights reserved.</div></footer><script src="/js/main.js" defer></script></body></html>'''
P={}
# HOME
VAL=[("Innovation","We continuously explore better ways to solve problems through technology."),("Practicality","We focus on solutions that serve genuine needs and deliver meaningful value."),("Excellence","We believe quality, reliability and attention to detail should guide everything we build."),("Integrity","We value honesty, responsibility and transparency in our work."),("Growth","We believe in continuous learning, improvement and building for the future.")]
FSTORY="Unisale Systems was founded by Collins Gitonga in October 2026, driven by a passion for technology, software development and the possibilities of automation."
showcase=f'<div class="showcase"><div><h2>Technology built for business.</h2><p class="lead">Unisale POS is a business management platform designed to help businesses manage sales, inventory, expenses and financial performance from one convenient system.</p>{hl()}<div class="row">{btn("/unisale-pos/","Explore Unisale POS")}{btn(REGISTER,"Get Started","line")}</div></div>'+(f'<div>{"".join(SHOTS[:2])}</div>' if SHOTS else f'<div class="brandpanel"><img src="{A}brand/unisale-systems-logo-full.png" alt="Unisale POS by Unisale Systems" width="600" height="250"></div>')+'</div>'
P["/"]=("Unisale Systems | Technology should make life easier","Unisale Systems builds practical digital products that simplify processes, automate everyday tasks and help businesses operate smarter.",
f'''<section class="hero"><div class="wrap"><div><h1>Technology should make life easier. We're building it that way.</h1><p class="lead">At Unisale Systems, we develop practical digital products that simplify processes, automate everyday tasks and help businesses operate smarter.</p><div class="row">{btn("/products/","Explore Our Products")}{btn("/our-story/","Discover Our Story","line")}</div></div>
<div class="brandpanel"><img src="{A}brand/unisale-systems-logo-full.png" alt="Unisale Systems logo" width="600" height="250"><p>Building technology. Automating possibilities.</p></div></div></section>
{sec('<div class="narrow"><h2>Technology with a purpose.</h2>'+paras("Technology is most valuable when it solves real problems. We believe in building solutions that make everyday processes simpler, businesses more efficient and opportunities more accessible.\n\nAt Unisale Systems, we turn ideas into practical digital products designed around real-world needs.")+'</div>')}
{sec('<h2>What we do</h2><div class="three"><div class="cap c1"><h3>Software Development</h3><p>We build practical software products and digital solutions designed to address real-world challenges.</p></div><div class="cap c2"><h3>Business Automation</h3><p>We develop systems that simplify repetitive tasks, improve workflows and help businesses operate more efficiently.</p></div><div class="cap c3"><h3>Digital Innovation</h3><p>We explore opportunities to apply technology in new and meaningful ways, creating products that make a difference.</p></div></div>',"sheet")}
{sec(showcase)}
{sec('<blockquote>We don\'t build technology just because we can. We build it because something can be done better.</blockquote><p>Every product begins with an opportunity to improve something. That belief shapes how we think, build and innovate.</p>',"phil")}
{sec(founder("<h2>The future is built, not waited for.</h2><p>"+FSTORY+"</p>"+f'<div class="row">{btn("/our-story/","Discover Our Story")}</div>'))}
{sec("<h2>Let's build something meaningful.</h2><p>Explore our products, discover what we're building or get in touch with the team.</p>"+f'<div class="row">{btn("/products/","Explore Products","onink go")}{btn("/contact/","Contact Us","onink")}</div>',"ink")}''')
# ABOUT
P["/about/"]=("Technology with purpose. Innovation with direction. | Unisale Systems","Unisale Systems is a technology company focused on practical digital products, software solutions and automation systems.",
head("Building technology that moves possibilities forward.","Unisale Systems is a technology company focused on creating practical digital products, software solutions and automation systems that simplify processes and improve how people and businesses operate.")+
sec('<div class="narrow"><h2>Who we are</h2>'+paras("Unisale Systems is built around a simple belief: technology should solve problems and create meaningful value.\n\nWe develop digital products and solutions with a focus on practicality, usability and innovation. Our goal is to transform ideas into useful technology that addresses real-world needs.\n\nWhile our journey begins with products designed to improve business operations, our ambition extends much further. We aim to build technology that can make an impact across industries and markets.")+'</div>')+
sec('<div class="three" style="grid-template-columns:1fr 1fr;margin:0"><div class="cap c1"><h2>Our Mission</h2><p>To develop innovative, practical and accessible technology solutions that simplify processes, empower businesses and create meaningful value.</p></div><div class="cap c2"><h2>Our Vision</h2><p>To become a globally recognised technology company known for building impactful digital products that transform how people work, operate businesses and interact with technology.</p></div></div>',"sheet")+
sec('<h2>Our Values</h2><div class="five">'+"".join(f"<div><h3>{h}</h3><p>{p}</p></div>" for h,p in VAL)+"</div>")+
sec(founder("<h2>Our Founder</h2>"+paras("Unisale Systems was founded by Collins Gitonga in October 2026. His passion for software development, technology and automation inspired the creation of a company focused on building useful digital products.\n\nWith a vision that extends beyond individual products, Unisale Systems represents a commitment to exploring how technology can make everyday processes better.")+f'<div class="row">{btn("/our-story/","Read Our Story")}</div>'),"sheet"))
# STORY
def story(h,t): return f'<div class="story"><h2>{h}</h2><div>{paras(t)}</div></div>'
P["/our-story/"]=("Every meaningful innovation starts with a question. | Unisale Systems","The story of Unisale Systems, founded in October 2026 by Collins Gitonga to build practical digital products.",
head("It started with a simple question: What if things could work better?","The story of Unisale Systems is rooted in a passion for building technology and exploring the possibilities of automation.")+
sec(story("An interest in possibilities.","Technology is more than code, computers or digital interfaces. It is an opportunity to rethink how things work.\n\nThe ability to turn an idea into a working product, simplify repetitive tasks and solve everyday problems is what makes technology exciting.\n\nThat interest in building and improving things became the foundation of Unisale Systems.")+
story("From an idea to a company.","Founded in October 2026 by Collins Gitonga, Unisale Systems was established with a clear ambition: to build practical digital products that solve real problems.\n\nRather than focusing on technology for its own sake, the company set out to develop solutions that people and businesses could actually use.")+
story("Our first step: Unisale POS.","Every technology company begins somewhere. For Unisale Systems, one of its first major steps is Unisale POS.\n\nDesigned to simplify business operations, Unisale POS brings essential business management capabilities into one platform.\n\nIt represents the company's belief that well-designed software can make everyday business processes easier and more efficient.")+(gallery() if SHOTS else "")+
story("Our story is still being written.","Unisale Systems is being built with a global perspective.\n\nOur ambition is to continue developing useful products, exploring new opportunities and applying technology to problems that matter.\n\nThe journey is only beginning."))+
sec(founder("<h2>Collins Gitonga</h2><p>Founder, Unisale Systems</p>"),"sheet"))
# PRODUCTS
P["/products/"]=("Products built to solve real problems. | Unisale Systems","Explore Unisale POS, the business management and point-of-sale platform from Unisale Systems.",
head("Technology designed around real-world needs.","We build digital products that simplify processes, improve productivity and make technology more useful.")+
sec(f'<div class="showcase"><div><h2>Unisale POS</h2><p class="lead">Unisale POS is a business management and point-of-sale platform designed to help businesses manage everyday operations from one system.</p>{hl()}<div class="row">{btn("/unisale-pos/","View Product")}{btn(REGISTER,"Get Started","line")}</div></div>'+(f'<div>{"".join(SHOTS[:2])}</div>' if SHOTS else f'<div class="brandpanel"><img src="{A}brand/unisale-systems-logo-full.png" alt="Unisale POS by Unisale Systems" width="600" height="250"></div>')+'</div>')+
sec("<h2>More possibilities ahead.</h2><p>We're continuously exploring new opportunities to build useful technology products, including software for healthcare such as hospital and clinical management. Unisale POS is part of a broader vision to develop solutions that make everyday work simpler and more efficient.</p>","sheet"))
# POS
def feat(h,t): return f'<div class="feat"><h3>{h}</h3><p>{t}</p></div>'
P["/unisale-pos/"]=("Run your business. Not the paperwork. | Unisale POS","Unisale POS gives businesses one platform for sales, inventory, expenses and financial performance. KES 3,000 installation, first month free.",
f'''<section class="hero"><div class="wrap"><div><h1>Run your business. Not the paperwork.</h1><p class="lead">Unisale POS gives businesses a convenient way to manage sales, inventory, expenses and financial performance through one integrated platform.</p><div class="row">{btn(REGISTER,"Get Started")}{btn(LOGIN,"Login","line")}</div></div>
<div class="receipt" aria-label="Sample till receipt"><h2>Your shop name</h2><small>Sample receipt</small><ul id="items"></ul><div class="tot"><span>Total</span><b id="total">KES 0</b></div><p class="ok" id="paid" hidden>Paid. Stock updated.</p></div></div></section>'''+
sec('<div class="two"><div><h2>Business management shouldn\'t be complicated.</h2></div><div><p>Managing sales, stock, expenses and business performance across disconnected processes can make daily operations unnecessarily difficult. Notebooks, spreadsheets and separate tools make it harder to see what is really happening in the business.</p></div></div>')+
sec('<div class="two"><div><h2>Everything working together.</h2></div><div><p>Unisale POS brings supported business operations into one platform, so a sale at the counter updates your stock and your reports without extra work.</p></div></div>',"sheet")+
sec("<h2>What you can do</h2>"+feat("Sales Management","Record sales at the terminal, take cash and M-Pesa payments, and open and close the register each day. Every sale produces a receipt, refunds are recorded against the original receipt, and the terminal has an offline mode that syncs when the connection returns.")+
feat("Inventory Management","Organise products by category and brand, watch stock levels, adjust stock when counts change, raise purchase orders with suppliers, receive goods and record supplier payments.")+
feat("Expense Management","Record cash taken out of the till for expenses and other payouts, so your cash position stays accurate.")+
feat("Financial Reporting","Review sales performance on the dashboard and download a sales report as a PDF.")+
feat("Branch Management","Run more than one location under one account and move stock between branches with transfers that are dispatched and received.")+
feat("Payment Integration","Accept cash and M-Pesa payments at the sale and keep a record of every transaction."))+
(sec("<h2>See it in action</h2>"+gallery(),"sheet") if SHOTS else "")+
sec('<h2>Who is it for?</h2><div class="five">'+"".join(f"<div><h3>{h}</h3></div>" for h in ["Retail businesses","Shops and stores","Small and growing businesses","Businesses managing inventory","Businesses that want better visibility into operations"])+"</div>")+
pricing()+
sec("<h2>Take control of your business operations.</h2><p>Start using Unisale POS and bring your essential business operations into one convenient platform.</p>"+f'<div class="row">{btn(REGISTER,"Create Account","onink go")}{btn(LOGIN,"Login","onink")}</div>',"ink"))
# CONTACT
fld=lambda n,l,i:f'<div class="f"><label for="{n}">{l}</label>{i}<span class="err" id="{n}-e" aria-live="polite"></span></div>'
opts="".join(f"<option>{o}</option>" for o in ["General Inquiry","Product Inquiry","Technical Support","Partnerships","Business Opportunities"])
P["/contact/"]=("Let's talk about possibilities. | Contact Unisale Systems","Get in touch with Unisale Systems about our products, support, partnerships or business opportunities.",
head("Have a question, idea or opportunity?","We'd love to hear from you. Whether you're interested in our products, need assistance or want to explore a potential collaboration, get in touch.")+
sec(f'<form class="form" id="cf" method="post" action="{FORM_ACTION}" novalidate>'+fld("name","Full Name",'<input id="name" name="name" autocomplete="name" required>')+fld("email","Email Address",'<input id="email" name="email" type="email" autocomplete="email" required>')+fld("subject","Subject",f'<select id="subject" name="subject" required><option value="">Choose a subject</option>{opts}</select>')+fld("message","Message",'<textarea id="message" name="message" rows="6" required></textarea>')+
'<input class="hp" name="website" tabindex="-1" autocomplete="off" aria-hidden="true"><div><button class="btn go" type="submit">Send message</button></div><p id="status" role="status"></p></form>'))
def legal(items): return "".join(f"<h2>{h}</h2><p>{p}</p>" for h,p in items)
P["/privacy-policy/"]=("Privacy Policy | Unisale Systems","How the Unisale Systems website handles your information.",head("Privacy Policy",f"Last updated: {LEGAL['date']}")+sec('<div class="narrow legal">'+legal([("Who we are",f"This website is operated by Unisale Systems ({LEGAL['entity']})."),("What we collect","Only the details you send through the contact form, and basic visit statistics if analytics is switched on."),("How we use it","To reply to you and to improve the website. We do not sell your information."),("The Unisale POS application","The application at app.unisalesystems.store may be subject to separate terms and privacy notices."),("Contact","Use the contact page to reach us about your information.")])+"</div>"))
P["/terms/"]=("Terms and Conditions | Unisale Systems","Terms for using the Unisale Systems website.",head("Terms and Conditions",f"Last updated: {LEGAL['date']}")+sec('<div class="narrow legal">'+legal([("Using this website","Use it lawfully and do not disrupt, misuse or attempt unauthorised access to it."),("Our content","Branding, content and design belong to Unisale Systems unless stated otherwise."),("No warranty","We aim to be accurate but provide the website as is, and may change it at any time."),("Unisale POS","Use of the application is governed by the terms presented in the application."),("Governing law",f"These terms are governed by {LEGAL['law']}.")])+"</div>"))
shutil.rmtree("dist",ignore_errors=True)
for d in("css","js","assets"): shutil.copytree(d,"dist/"+d)
for path,(t,d,b) in P.items():
    os.makedirs("dist"+path,exist_ok=True); open("dist"+path+"index.html","w").write(page(path,t,d,b))
open("dist/404.html","w").write(page("/404.html","Page not found | Unisale Systems","Page not found.",head("Page not found","That page does not exist.")+sec(btn("/","Back to home"))))
open("dist/sitemap.xml","w").write('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+"".join(f"<url><loc>{DOMAIN+p}</loc></url>" for p in P)+"</urlset>")
open("dist/robots.txt","w").write(f"User-agent: *\nAllow: /\n\nSitemap: {DOMAIN}/sitemap.xml\n")
print("built",len(P),"pages; screenshots:",len(SHOTS),"; founder photo:",exists("founder/collins-gitonga.jpg"),"; form endpoint set:",bool(FORM_ACTION))
