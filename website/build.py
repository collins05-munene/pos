import os, json, html
# ---- CONFIG (edit, then run: python3 build.py) ----
DOMAIN="https://unisalesystems.store"
LOGIN="https://app.unisalesystems.store/users/login/"
REGISTER="https://app.unisalesystems.store/signup/"
FORM_ACTION=""            # e.g. Formspree/your endpoint URL. Leave empty until you have one.
GA_ID=""                  # e.g. G-XXXXXXXXXX
GSC_TOKEN=""; BING_TOKEN=""  # verification codes
CONTACT_EMAIL=""          # only shown if set
SOCIALS={}                # {"LinkedIn":"https://..."}
LEGAL={"entity":"[Legal entity name – to be confirmed]","address":"[Registered address – to be confirmed]","email":"[Privacy contact email – to be confirmed]","law":"[Governing law – to be confirmed]"}
A="/assets/"
SHOTS=[("products/unisale-pos/dashboard.png","Unisale POS dashboard"),("products/unisale-pos/sales.png","Unisale POS sales screen"),("products/unisale-pos/inventory.png","Unisale POS inventory management screen"),("products/unisale-pos/reports.png","Unisale POS financial reporting screen"),("products/unisale-pos/expenses.png","Unisale POS expense management screen")]
NAV=[("/","Home"),("/about/","About Us"),("/our-story/","Our Story"),("/products/","Products"),("/contact/","Contact")]
def img(p,alt,cls="",lazy=True):
    return f'<figure class="shot {cls}"><img src="{A}{p}" alt="{alt}" {"loading=lazy " if lazy else ""}decoding="async" onerror="this.closest(\'figure\').hidden=true"></figure>'
def shots(n=None): return '<div class="gallery">'+"".join(img(p,a) for p,a in SHOTS[:n])+'</div>'
def founder(): return f'<figure class="portrait"><img src="{A}founder/collins-gitonga.jpg" alt="Collins Gitonga, founder of Unisale Systems" loading="lazy" onerror="this.closest(\'figure\').hidden=true"></figure>'
def btn(h,t,c="primary"): return f'<a class="btn {c}" href="{h}">{t}</a>'
def sec(inner,cls="",id=""): return f'<section class="{cls}"{f" id={id}" if id else ""}><div class="wrap">{inner}</div></section>'
def paras(t): return "".join(f"<p>{x}</p>" for x in t.strip().split("\n\n"))
def cards(items,cls=""): return f'<div class="cards {cls}">'+"".join(f"<article class=card><h3>{h}</h3><p>{p}</p></article>" for h,p in items)+"</div>"
ORG={"@type":"Organization","@id":DOMAIN+"/#organization","name":"Unisale Systems","url":DOMAIN+"/","logo":DOMAIN+A+"brand/unisale-systems-logo.png","foundingDate":"2026-10","slogan":"Building technology. Automating possibilities.","founder":{"@id":DOMAIN+"/#founder"}}
if SOCIALS: ORG["sameAs"]=list(SOCIALS.values())
PERSON={"@type":"Person","@id":DOMAIN+"/#founder","name":"Collins Gitonga","jobTitle":"Founder","worksFor":{"@id":DOMAIN+"/#organization"}}
SITE={"@type":"WebSite","@id":DOMAIN+"/#website","url":DOMAIN+"/","name":"Unisale Systems","publisher":{"@id":DOMAIN+"/#organization"}}
APP={"@type":"SoftwareApplication","@id":DOMAIN+"/unisale-pos/#software","name":"Unisale POS","applicationCategory":"BusinessApplication","operatingSystem":"Web","url":DOMAIN+"/unisale-pos/","description":"A business management and point-of-sale platform for managing sales, inventory, expenses and financial performance.","author":{"@id":DOMAIN+"/#organization"},"publisher":{"@id":DOMAIN+"/#organization"}}
def crumbs(path,name):
    it=[("Home","/")]+([] if path=="/" else [(name,path)])
    return {"@type":"BreadcrumbList","itemListElement":[{"@type":"ListItem","position":i+1,"name":n,"item":DOMAIN+p} for i,(n,p) in enumerate(it)]}
def page(path,title,desc,body,name,ld=(),og="website"):
    url=DOMAIN+path
    graph=[ORG,SITE,PERSON]+list(ld)+([crumbs(path,name)] if path!="/" else [])
    nav="".join(f'<li><a href="{h}"{" aria-current=page" if h==path else ""}>{t}</a></li>' for h,t in NAV)
    soc="".join(f'<a href="{u}" rel="noopener me">{n}</a> ' for n,u in SOCIALS.items())
    ga=f'<script async src="https://www.googletagmanager.com/gtag/js?id={GA_ID}"></script><script>window.dataLayer=[];function gtag(){{dataLayer.push(arguments)}}gtag("js",new Date());gtag("config","{GA_ID}")</script>' if GA_ID else ""
    ver=(f'<meta name="google-site-verification" content="{GSC_TOKEN}">' if GSC_TOKEN else "")+(f'<meta name="msvalidate.01" content="{BING_TOKEN}">' if BING_TOKEN else "")
    ogimg=DOMAIN+A+"brand/og-image.png"
    bc="" if path=="/" else f'<nav class="crumbs wrap" aria-label="Breadcrumb"><a href="/">Home</a> / <span>{name}</span></nav>'
    return f'''<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="description" content="{html.escape(desc)}"><link rel="canonical" href="{url}">{ver}
<meta property="og:type" content="{og}"><meta property="og:site_name" content="Unisale Systems"><meta property="og:title" content="{html.escape(title)}"><meta property="og:description" content="{html.escape(desc)}"><meta property="og:url" content="{url}"><meta property="og:image" content="{ogimg}">
<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{html.escape(title)}"><meta name="twitter:description" content="{html.escape(desc)}"><meta name="twitter:image" content="{ogimg}">
<link rel="icon" href="{A}icons/favicon.png" type="image/png"><link rel="icon" href="{A}icons/favicon.png" sizes="any">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Space+Grotesk:wght@500;600;700&display=swap"><link rel="stylesheet" href="/css/style.css">
<script type="application/ld+json">{json.dumps({"@context":"https://schema.org","@graph":graph})}</script>{ga}</head><body>
<a class="skip" href="#main">Skip to content</a>
<header class="top"><div class="wrap bar"><a class="brand" href="/" aria-label="Unisale Systems home"><img src="{A}brand/unisale-systems-logo.png" alt="" height="28" onerror="this.hidden=true"><span>Unisale Systems</span></a>
<button class="burger" aria-expanded="false" aria-controls="menu" aria-label="Menu"><span></span><span></span></button>
<div class="menu" id="menu"><ul>{nav}</ul><div class="acts">{btn(LOGIN,"Login","ghost")}{btn(REGISTER,"Get Started")}</div></div></div></header>
<main id="main">{bc}{body}</main>
<footer class="foot"><div class="wrap fgrid"><div><a class="brand" href="/"><img src="{A}brand/unisale-systems-logo-light.png" alt="" height="28" onerror="this.hidden=true"><span>Unisale Systems</span></a><p>Unisale Systems is a technology company building practical digital products, software solutions and automation systems.</p>{soc}</div>
<div><h2>Company</h2><ul><li><a href="/about/">About Us</a></li><li><a href="/our-story/">Our Story</a></li><li><a href="/contact/">Contact</a></li></ul></div>
<div><h2>Products</h2><ul><li><a href="/products/">All products</a></li><li><a href="/unisale-pos/">Unisale POS</a></li><li><a href="{LOGIN}">Login</a></li><li><a href="{REGISTER}">Get Started</a></li></ul></div>
<div><h2>Legal</h2><ul><li><a href="/privacy-policy/">Privacy Policy</a></li><li><a href="/terms/">Terms and Conditions</a></li></ul></div></div>
<div class="wrap copy">© 2026 Unisale Systems. All rights reserved.</div></footer><script src="/js/main.js" defer></script></body></html>'''
def hero(h,p,extra="",dark=True,h1="h1"): return f'<section class="hero {"dark" if dark else "light"}"><div class="wrap"><{h1}>{h}</{h1}><p class="lead">{p}</p>{extra}</div></section>'
PH="Collins Gitonga"
FEATS=[("Sales management","Record sales and manage transactions from one place."),("Inventory management","Keep track of products and stock levels."),("Expense management","Record and monitor operating expenses."),("Financial reporting","Review business performance through available reports."),("Branch management","Manage supported multi-branch operations."),("Payment integration","Work with the payment methods currently supported in the application.")]
P={}
# HOME
P["/"]=("Unisale Systems | Building Practical Technology Solutions","Unisale Systems is a technology company founded by Collins Gitonga, building practical software and automation products, including the Unisale POS business management platform.","Home",[],
 f'''<section class="hero dark home"><div class="wrap split"><div><h1>Technology should make life easier. We're building it that way.</h1><p class="lead">At Unisale Systems, we develop practical digital products that simplify processes, automate everyday tasks and help businesses operate smarter.</p><div class="row">{btn("/products/","Explore Our Products")}{btn("/our-story/","Discover Our Story","ghost-dark")}</div></div>
<div class="mark" aria-hidden="true"><div class="tag">Building technology.<br>Automating possibilities.</div></div></div></section>
{sec("<div class=narrow><h2>Technology with a purpose.</h2>"+paras("Technology is most valuable when it solves real problems. We believe in building solutions that make everyday processes simpler, businesses more efficient and opportunities more accessible.\n\nAt Unisale Systems, we turn ideas into practical digital products designed around real-world needs.")+"</div>","pad")}
{sec("<h2>What we do</h2>"+cards([("Software Development","We build practical software products and digital solutions designed to address real-world challenges."),("Business Automation","We develop systems that simplify repetitive tasks, improve workflows and help businesses operate more efficiently."),("Digital Innovation","We explore opportunities to apply technology in new and meaningful ways, creating products that make a difference.")],"three"),"pad alt")}
<section class="pad dark"><div class="wrap"><h2>Technology built for business.</h2><p class="lead">Unisale POS is a business management platform designed to help businesses manage sales, inventory, expenses and financial performance from one convenient system.</p><ul class="chips">{"".join(f"<li>{t}</li>" for t,_ in FEATS[:4])}</ul>{shots(2)}<div class="row">{btn("/unisale-pos/","Explore Unisale POS")}{btn(REGISTER,"Get Started","ghost-dark")}</div></div></section>
{sec('<blockquote class="quote"><p>We don\'t build technology just because we can. We build it because something can be done better.</p></blockquote><p class="center">Every product begins with an opportunity to improve something. That belief shapes how we think, build and innovate.</p>',"pad phil")}
{sec(f'<div class="split2">{founder()}<div><h2>The future is built, not waited for.</h2><p>Unisale Systems was founded by Collins Gitonga in October 2026, driven by a passion for technology, software development and the possibilities of automation.</p>{btn("/our-story/","Discover Our Story")}</div></div>',"pad alt")}
<section class="pad dark cta"><div class="wrap center"><h2>Let's build something meaningful.</h2><p class="lead">Explore our products, discover what we're building or get in touch with the team.</p><div class="row c">{btn("/products/","Explore Products")}{btn("/contact/","Contact Us","ghost-dark")}</div></div></section>''')
# ABOUT
vals=[("Innovation","We continuously explore better ways to solve problems through technology."),("Practicality","We focus on solutions that serve genuine needs and deliver meaningful value."),("Excellence","We believe quality, reliability and attention to detail should guide everything we build."),("Integrity","We value honesty, responsibility and transparency in our work."),("Growth","We believe in continuous learning, improvement and building for the future.")]
P["/about/"]=("About Unisale Systems | Our Mission and Vision","Learn about Unisale Systems, a technology company building practical digital products and automation systems. Read our mission, vision, values and founder.","About Us",[],
 hero("Building technology that moves possibilities forward.","Unisale Systems is a technology company focused on creating practical digital products, software solutions and automation systems that simplify processes and improve how people and businesses operate.")+
 sec("<div class=narrow><h2>Who we are</h2>"+paras("Unisale Systems is built around a simple belief: technology should solve problems and create meaningful value.\n\nWe develop digital products and solutions with a focus on practicality, usability and innovation. Our goal is to transform ideas into useful technology that addresses real-world needs.\n\nWhile our journey begins with products designed to improve business operations, our ambition extends much further. We aim to build technology that can make an impact across industries and markets.")+"</div>","pad")+
 sec('<div class="cards two"><article class="card"><h2>Our Mission</h2><p>To develop innovative, practical and accessible technology solutions that simplify processes, empower businesses and create meaningful value.</p></article><article class="card"><h2>Our Vision</h2><p>To become a globally recognised technology company known for building impactful digital products that transform how people work, operate businesses and interact with technology.</p></article></div>',"pad alt")+
 sec("<h2>Our Values</h2>"+cards(vals,"five"),"pad")+
 sec(f'<div class="split2">{founder()}<div><h2>Our Founder</h2><p>Unisale Systems was founded by Collins Gitonga in October 2026. His passion for software development, technology and automation inspired the creation of a company focused on building useful digital products.</p><p>With a vision that extends beyond individual products, Unisale Systems represents a commitment to exploring how technology can make everyday processes better.</p>{btn("/our-story/","Read Our Story")}</div></div>',"pad alt"))
# STORY
P["/our-story/"]=("Our Story | The Journey Behind Unisale Systems","The story of Unisale Systems: founded in October 2026 by Collins Gitonga to build practical digital products, starting with Unisale POS.","Our Story",[],
 hero("It started with a simple question: What if things could work better?","The story of Unisale Systems is rooted in a passion for building technology and exploring the possibilities of automation.")+
 sec("<div class=narrow><h2>An interest in possibilities.</h2>"+paras("Technology is more than code, computers or digital interfaces. It is an opportunity to rethink how things work.\n\nThe ability to turn an idea into a working product, simplify repetitive tasks and solve everyday problems is what makes technology exciting.\n\nThat interest in building and improving things became the foundation of Unisale Systems.")+"</div>","pad")+
 sec("<div class=narrow><h2>From an idea to a company.</h2>"+paras("Founded in October 2026 by Collins Gitonga, Unisale Systems was established with a clear ambition: to build practical digital products that solve real problems.\n\nRather than focusing on technology for its own sake, the company set out to develop solutions that people and businesses could actually use.")+"</div>","pad alt")+
 sec("<div class=narrow><h2>Our first step: Unisale POS.</h2>"+paras("Every technology company begins somewhere. For Unisale Systems, one of its first major steps is Unisale POS.\n\nDesigned to simplify business operations, Unisale POS brings essential business management capabilities into one platform.\n\nIt represents the company's belief that well-designed software can make everyday business processes easier and more efficient.")+"</div>"+shots(2),"pad")+
 sec("<div class=narrow><h2>Our story is still being written.</h2>"+paras("Unisale Systems is being built with a global perspective.\n\nOur ambition is to continue developing useful products, exploring new opportunities and applying technology to problems that matter.\n\nThe journey is only beginning.")+"</div>","pad alt")+
 sec(f'<div class="split2">{founder()}<div><h2>{PH}</h2><p>Founder, Unisale Systems</p></div></div>',"pad"))
# PRODUCTS
P["/products/"]=("Our Products | Unisale Systems","Explore the digital products built by Unisale Systems, including Unisale POS, a business management and point-of-sale platform.","Products",[APP],
 hero("Technology designed around real-world needs.","We build digital products that simplify processes, improve productivity and make technology more useful.")+
 sec(f'<h2>Unisale POS</h2><p class="lead dk">Unisale POS is a business management and point-of-sale platform designed to help businesses manage everyday operations from one system.</p><ul class="chips lt">{"".join(f"<li>{t}</li>" for t,_ in FEATS)}</ul>{shots(2)}<div class="row">{btn("/unisale-pos/","View Product")}{btn(REGISTER,"Get Started","ghost")}</div>',"pad")+
 sec("<h2>More possibilities ahead.</h2><p>We're continuously exploring new opportunities to build useful technology products. Unisale POS is part of a broader vision to develop solutions that make everyday work simpler and more efficient.</p>","pad alt"))
# POS
P["/unisale-pos/"]=("Unisale POS | Business Management Software","Unisale POS is business management and point-of-sale software for managing sales, inventory, expenses and financial performance from one platform.","Unisale POS",[APP],
 hero("Run your business. Not the paperwork.","Unisale POS gives businesses a convenient way to manage sales, inventory, expenses and financial performance through one integrated platform.",f'<div class="row">{btn(REGISTER,"Get Started")}{btn(LOGIN,"Login","ghost-dark")}</div>'+shots(1))+
 sec("<div class=narrow><h2>Business management shouldn't be complicated.</h2><p>Managing sales, stock, expenses and business performance across disconnected processes can make daily operations unnecessarily difficult.</p></div>","pad")+
 sec("<div class=narrow><h2>Everything working together.</h2><p>Unisale POS brings supported business operations into one platform, so sales, stock, expenses and reports live together.</p></div>","pad alt")+
 sec("<h2>Features</h2>"+cards(FEATS,"three"),"pad")+
 sec("<h2>Product screenshots</h2>"+shots(),"pad alt")+
 sec("<h2>Who is it for?</h2>"+cards([("Retail businesses and shops","Record sales and manage everyday trading."),("Small and growing businesses","Bring essential operations into one place."),("Businesses managing inventory","Keep products and stock organised."),("Owners wanting visibility","Review expenses and financial performance.")],"four"),"pad")+
 f'<section class="pad dark cta"><div class="wrap center"><h2>Take control of your business operations.</h2><p class="lead">Start using Unisale POS and bring your essential business operations into one convenient platform.</p><div class="row c">{btn(REGISTER,"Create Account")}{btn(LOGIN,"Login","ghost-dark")}</div></div></section>')
# CONTACT
opts="".join(f"<option>{o}</option>" for o in ["General Inquiry","Product Inquiry","Technical Support","Partnerships","Business Opportunities"])
ce=f'<p>Email: <a href="mailto:{CONTACT_EMAIL}">{CONTACT_EMAIL}</a></p>' if CONTACT_EMAIL else ""
P["/contact/"]=("Contact Unisale Systems | Let's Connect","Contact Unisale Systems about our products, support, partnerships or business opportunities.","Contact",[],
 hero("Have a question, idea or opportunity?","We'd love to hear from you. Whether you're interested in our products, need assistance or want to explore a potential collaboration, get in touch.")+
 sec(f'''<div class="narrow">{ce}<form id="cf" class="form" method="post" action="{FORM_ACTION}" novalidate>
<div class="f"><label for="name">Full Name</label><input id="name" name="name" autocomplete="name" required minlength="2" maxlength="100"><small class="err" id="e-name"></small></div>
<div class="f"><label for="email">Email Address</label><input id="email" name="email" type="email" autocomplete="email" required maxlength="150"><small class="err" id="e-email"></small></div>
<div class="f"><label for="subject">Subject</label><select id="subject" name="subject" required><option value="">Select a subject</option>{opts}</select><small class="err" id="e-subject"></small></div>
<div class="f"><label for="message">Message</label><textarea id="message" name="message" rows="6" required minlength="10" maxlength="3000"></textarea><small class="err" id="e-message"></small></div>
<div class="hp" aria-hidden="true"><label>Leave empty<input name="_gotcha" tabindex="-1" autocomplete="off"></label></div>
<button class="btn primary" type="submit">Send message</button><p id="status" role="status" aria-live="polite"></p></form></div>''',"pad"))
LG=LEGAL
def legal(items): return "".join(f"<h2>{h}</h2><p>{t}</p>" for h,t in items)
P["/privacy-policy/"]=("Privacy Policy | Unisale Systems","How Unisale Systems collects, uses and protects personal information on its website.","Privacy Policy",[],
 hero("Privacy Policy","Last updated: [date – to be confirmed]",h1="h1")+sec("<div class='narrow legal'>"+legal([("Who we are",f"This website is operated by Unisale Systems ({LG['entity']}), {LG['address']}. Contact: {LG['email']}."),("Information we collect","Information you submit through the contact form (name, email address, subject and message), and basic technical data such as browser type and pages visited if analytics is enabled."),("How we use it","To respond to enquiries, operate and improve the website, and keep it secure. We do not sell personal information."),("Cookies and analytics","The website may use analytics to understand usage. Where analytics is enabled, it is disclosed here."),("Sharing","We may share information with service providers who help us operate the website, or where required by law."),("Retention and security","We keep information only as long as needed for the purposes above and use reasonable measures to protect it."),("Your rights","Depending on your location you may request access, correction or deletion of your personal information by contacting us."),("The Unisale POS application","The Unisale POS application at app.unisalesystems.store may be subject to separate terms and privacy notices."),("Changes","We may update this policy and will publish the current version on this page.")])+"</div>","pad"))
P["/terms/"]=("Terms and Conditions | Unisale Systems","The terms and conditions for using the Unisale Systems website.","Terms and Conditions",[],
 hero("Terms and Conditions","Last updated: [date – to be confirmed]")+sec("<div class='narrow legal'>"+legal([("Acceptance",f"By using this website, operated by Unisale Systems ({LG['entity']}), you agree to these terms."),("Use of the website","You agree to use the website lawfully and not to disrupt, misuse or attempt unauthorised access to it."),("Intellectual property","Content, branding and design on this website belong to Unisale Systems unless stated otherwise and may not be reused without permission."),("Information provided","We aim for accurate content but provide the website as is, without warranties, and may change it at any time."),("Unisale POS","Use of the Unisale POS application is governed by the terms presented in that application."),("Limitation of liability","To the extent permitted by law, Unisale Systems is not liable for losses arising from use of this website."),("Governing law",f"These terms are governed by {LG['law']}."),("Contact",f"Questions about these terms: {LG['email']}.")])+"</div>","pad"))
os.makedirs("dist",exist_ok=True)
import shutil
for d in ("css","js","assets"):
    shutil.copytree(d,"dist/"+d,dirs_exist_ok=True)
urls=[]
for path,(t,d,n,ld,b) in P.items():
    o="dist"+path+("" if path=="/" else "")
    os.makedirs(o,exist_ok=True)
    open(o+"/index.html","w").write(page(path,t,d,b,n,ld))
    urls.append(DOMAIN+path)
open("dist/sitemap.xml","w").write('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'+"".join(f"<url><loc>{u}</loc></url>" for u in urls)+"</urlset>")
open("dist/robots.txt","w").write(f"User-agent: Googlebot\nAllow: /\n\nUser-agent: Bingbot\nAllow: /\n\nUser-agent: OAI-SearchBot\nAllow: /\n\nUser-agent: *\nAllow: /\n\nSitemap: {DOMAIN}/sitemap.xml\n")
open("dist/404.html","w").write(page("/404.html","Page not found | Unisale Systems","Page not found.",hero("Page not found","The page you're looking for doesn't exist. "+btn("/","Back home")),"Not found"))
print("built",len(urls),"pages")
