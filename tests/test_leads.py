import unittest

from crawler.leads import contact_links, contacts, norm_phone


class Leads(unittest.TestCase):
    def test_phones_and_socials(self):
        html = """<p>تلفن: ۰۲۱-۴۴۵۵۶۶۷۷ موبایل: ۰۹۱۲ ۳۴۵ ۶۷۸۹ و +98 935 111 2233</p>
        <a href="https://instagram.com/noghrab_pool/">اینستا</a> <a href="https://www.instagram.com/p/xyz">post</a>
        <a href="https://t.me/noghrabpool">تلگرام</a> <a href="https://wa.me/989121234567">واتساپ</a>
        <a href="https://eitaa.com/noghrab">ایتا</a> info@noghrab.com logo@2x.png"""
        c = contacts(html)
        self.assertEqual(c["mobiles"], ["09121234567", "09123456789", "09351112233"])   # the WhatsApp number counts
        self.assertEqual(c["landlines"], ["02144556677"])
        self.assertEqual(c["instagram"], ["noghrab_pool"])
        self.assertEqual(c["telegram"], ["noghrabpool"])
        self.assertEqual(c["whatsapp"], ["989121234567"])
        self.assertEqual(c["eitaa"], ["noghrab"])
        self.assertEqual(c["email"], ["info@noghrab.com"])

    def test_norm_phone(self):
        self.assertEqual(norm_phone("+98 21 4455 6677"), "02144556677")
        self.assertEqual(norm_phone("۰۹۱۲۳۴۵۶۷۸۹"), "09123456789")
        self.assertEqual(norm_phone(""), "")

    def test_contact_links_same_site_only(self):
        html = '<a href="/contact-us">x</a><a href="/shop">فروشگاه</a><a href="/about">درباره ما</a>' \
               '<a href="https://other.com/contact">c</a>'
        self.assertEqual(contact_links("https://a.ir/", html), ["https://a.ir/contact-us", "https://a.ir/about"])


if __name__ == "__main__":
    unittest.main()
