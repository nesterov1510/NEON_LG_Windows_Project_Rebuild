from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_contact_card_and_qr_assets_are_packaged():
    card = (ROOT / 'assets/tvrepair.vcf').read_text(encoding='utf-8')
    for value in ('BEGIN:VCARD', 'END:VCARD', '+99363705953', 'tvrepairmsb@gmail.com',
                  'https://tvrepair.meryosab.com/', 'https://wa.me/99363705953',
                  'https://t.me/meryosab_shop'):
        assert value in card
    for filename in ('msb_website_qr.png', 'msb_contact_qr.png'):
        assert (ROOT / 'assets' / filename).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    html = (ROOT / 'ui.html').read_text(encoding='utf-8')
    assert 'id="callbackPage"' in html
    assert 'assets/msb_contact_qr.png' in html
    assert 'assets/msb_website_qr.png' in html
