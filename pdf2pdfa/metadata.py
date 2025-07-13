"""XMP metadata generation for PDF/A."""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Dict

from lxml import etree

logger = logging.getLogger(__name__)


_NS = {
    'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
    'pdfaid': 'http://www.aiim.org/pdfa/ns/id/',
    'xmp': 'http://ns.adobe.com/xap/1.0/',
    'dc': 'http://purl.org/dc/elements/1.1/',
}


def generate_xmp_metadata() -> str:
    """Return a minimal PDF/A-1b compliant XMP packet."""
    logger.debug("Generating XMP metadata")
    now = _dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
    rdf = etree.Element('{%s}RDF' % _NS['rdf'], nsmap=_NS)
    desc = etree.SubElement(rdf, '{%s}Description' % _NS['rdf'])
    desc.set('{%s}about' % _NS['rdf'], '')

    pdfaid = etree.SubElement(desc, '{%s}part' % _NS['pdfaid'])
    pdfaid.text = '1'
    conf = etree.SubElement(desc, '{%s}conformance' % _NS['pdfaid'])
    conf.text = 'B'

    created = etree.SubElement(desc, '{%s}CreateDate' % _NS['xmp'])
    created.text = now
    mod = etree.SubElement(desc, '{%s}ModifyDate' % _NS['xmp'])
    mod.text = now
    tool = etree.SubElement(desc, '{%s}CreatorTool' % _NS['xmp'])
    tool.text = 'pdf2pdfa'
    prod = etree.SubElement(desc, '{%s}Producer' % _NS['xmp'])
    prod.text = 'pdf2pdfa'

    dc_format = etree.SubElement(desc, '{%s}format' % _NS['dc'])
    dc_format.text = 'application/pdf'

    xml_bytes = etree.tostring(
        rdf,
        xml_declaration=False,
        encoding='utf-8',
        pretty_print=False,
    )
    packet = (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        + xml_bytes.decode('utf-8') +
        '\n<?xpacket end="w"?>'
    )
    return packet

