"""Regenerate the offline TV Repair QR images (requires qrcode and Pillow)."""
from pathlib import Path
import qrcode

ROOT = Path(__file__).resolve().parent.parent
VCARD = ('BEGIN:VCARD\r\nVERSION:3.0\r\nN:;TV Repair by Meryosab;;;\r\n'
         'FN:TV Repair by Meryosab\r\nORG:TV Repair by Meryosab\r\n'
         'TEL;TYPE=WORK,VOICE:+99363705953\r\nEMAIL;TYPE=WORK:tvrepairmsb@gmail.com\r\n'
         'ADR;TYPE=WORK:;;Parahat 3/2\\, dom 14;Ashgabat;;;Turkmenistan\r\n'
         'URL:https://tvrepair.meryosab.com/\r\n'
         'NOTE:WhatsApp https://wa.me/99363705953 | Telegram https://t.me/meryosab_shop\r\n'
         'END:VCARD\r\n')


def create():
    for name, data in [('msb_website_qr.png', 'https://tvrepair.meryosab.com/'), ('msb_contact_qr.png', VCARD)]:
        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=4)
        qr.add_data(data)
        qr.make(fit=True)
        qr.make_image(fill_color='black', back_color='white').save(ROOT / 'assets' / name)
    (ROOT / 'assets' / 'tvrepair.vcf').write_bytes(VCARD.encode('utf-8'))


if __name__ == '__main__':
    create()
