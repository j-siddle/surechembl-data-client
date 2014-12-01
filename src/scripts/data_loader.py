import logging
import codecs
import json
import csv
from datetime import datetime
from sqlalchemy import MetaData, Table, ForeignKey, Column, Sequence, Integer, Float, String, SmallInteger, Date, Text, select

logger = logging.getLogger(__name__)
logging.basicConfig( format='%(asctime)s %(levelname)s %(name)s %(message)s', level=logging.INFO)

class DocumentField:

    DESCRIPTION = 1
    CLAIMS      = 2
    ABSTRACT    = 3
    TITLE       = 4
    IMAGES      = 5
    ATTACHMENTS = 6

class DocumentClass:

    IPC  = 1
    ECLA = 2
    IPCR = 3
    CPC  = 4

    bib_dict = {
        'ipc'  : IPC,
        'ecla' : ECLA,
        'ipcr' : IPCR,
        'cpc'  : CPC }




class DataLoader:

    def __init__(self, db):
        self.db = db
        self.metadata = MetaData()
        self.doc_id_map = dict()
        self.existing_chemicals = set()

        # TODO field sizes asserted - all tables / fields
        # TODO FK and Nullable tested - all tables
        self.docs = Table('schembl_document', self.metadata,
                     Column('id',                Integer,       Sequence('schembl_document_id'), primary_key=True),
                     Column('scpn',              String(50),    unique=True),
                     Column('published',         Date()),
                     Column('life_sci_relevant', SmallInteger()),
                     Column('family_id',         Integer))

        self.titles = Table('schembl_document_title', self.metadata,
                     Column('schembl_doc_id',    Integer,       ForeignKey('schembl_document.id'), primary_key=True),
                     Column('lang',              String(10),    primary_key=True),
                     Column('text',              Text()))

        self.classes = Table('schembl_document_class', self.metadata,
                     Column('schembl_doc_id',    Integer,        ForeignKey('schembl_document.id'), primary_key=True),
                     Column('class',             String(100),    primary_key=True),
                     Column('system',            SmallInteger(), primary_key=True))

        self.chemicals = Table('schembl_chemical', self.metadata,
                     Column('id',                Integer,        primary_key=True),
                     Column('mol_weight',        Float()),
                     Column('logp',              Float()),
                     Column('med_chem_alert',    SmallInteger()),
                     Column('is_relevant',       SmallInteger()),
                     Column('donor_count',       SmallInteger()),
                     Column('acceptor_count',    SmallInteger()),
                     Column('ring_count',        SmallInteger()),
                     Column('rot_bond_count',    SmallInteger()),
                     Column('corpus_count',      Integer()))

        self.chem_structures = Table('schembl_chemical_structure', self.metadata,
                     Column('schembl_chem_id',   Integer,   ForeignKey('schembl_chemical.id'), primary_key=True),
                     Column('smiles',            Text()),
                     Column('std_inchi',         Text()),
                     Column('std_inchikey',      String(27)))

        self.chem_mapping = Table('schembl_document_chemistry', self.metadata,
                     Column('schembl_doc_id',   Integer,      ForeignKey('schembl_document.id'), primary_key=True),
                     Column('schembl_chem_id',  Integer,      ForeignKey('schembl_chemical.id'), primary_key=True),
                     Column('field',            SmallInteger, primary_key=True),
                     Column('frequency',        Integer))

    def db_metadata(self):
        return self.metadata


    def load_biblio(self, file_name):

        logger.info( "Loading biblio from [{}]".format(file_name) )

        input_file = codecs.open(file_name, 'r', 'utf-8')
        biblio = json.load(input_file)

        conn = self.db.connect()
        doc_ins = self.docs.insert()
        title_ins = self.titles.insert()
        class_ins = self.classes.insert()

        # TODO retrieve known document IDs (configurable)

        for chunk in chunks(biblio, 1000):

            # TODO Multiple inserts?
            for bib in chunk:

                transaction = conn.begin()

                # TODO missing / empty values rejected (or explicitly allowed)

                pubnumber = bib_scalar(bib, 'pubnumber')
                pubdate = datetime.strptime( bib_scalar( bib,'pubdate'), '%Y%m%d')

                record = dict(
                    scpn              = pubnumber,
                    published         = pubdate,
                    family_id         = bib_scalar(bib, 'family_id'),
                    life_sci_relevant = 1 )

                # TODO duplicate SCPN
                # TODO life science relevant function

                result = conn.execute(doc_ins, record)

                doc_id = result.inserted_primary_key[0] # Single PK

                self.doc_id_map[pubnumber] = doc_id


                # TODO missing titles handled
                # TODO missing titles field handled
                titles = bib['title']
                for i, title_lang in enumerate( bib['title_lang'] ):
                    title = titles[i]

                    record = dict(
                        schembl_doc_id = doc_id,
                        lang           = title_lang,
                        text           = title)

                    result = conn.execute(title_ins, record)

                # TODO missing classification fields handled
                for system_key in ['ipc','ecla','ipcr','cpc']:

                    for classif in bib[system_key]:

                        record = {
                            'schembl_doc_id': doc_id,
                            'class'         : classif,
                            'system'        : DocumentClass.bib_dict[system_key] }

                        result = conn.execute(class_ins, record)

                transaction.commit()

        conn.close()
        input_file.close()

    def load_chems(self, file_name, chunksize=1000):

        logger.info( "Loading chemicals from [{}]".format(file_name) )

        csv.field_size_limit(10000000)

        input_file = codecs.open(file_name, 'rb', 'utf-8')
        tsvin = csv.reader(input_file, delimiter='\t')

        conn = self.db.connect()
        chem_ins = self.chemicals.insert()
        chem_struc_ins = self.chem_structures.insert()
        chem_map_ins = self.chem_mapping.insert()

        chunk = []

        for i, row in enumerate(tsvin):

            if (i == 0):
                # TODO verify header?
                continue

            if (i % chunksize == 0 and i > 0):
                logger.info( "Processing chunk to index {}".format(i) )
                self.process_chem_rows(conn, chem_ins, chem_struc_ins, chem_map_ins, chunk)
                del chunk[:]

            chunk.append(row)

        logger.info( "Processing chunk to index {} (final)".format(i) )
        self.process_chem_rows(conn, chem_ins, chem_struc_ins, chem_map_ins, chunk)


    def process_chem_rows(self, conn, chem_ins, chem_struc_ins, chem_map_ins, rows):

        chem_ids = set()
        for row in rows:
            chem_id = int(row[1])
            if chem_id in self.existing_chemicals:
                continue
            chem_ids.add( chem_id )

        sel = select(
                [self.chemicals.c.id])\
              .where(
                (self.chemicals.c.id.in_(chem_ids) ))

        result = conn.execute(sel)
        found_chems = result.fetchall()
        for found_chem in found_chems:
            self.existing_chemicals.add( found_chem[0] )

        transaction = conn.begin()

        for i, row in enumerate(rows):

            doc_id  = self.doc_id_map[ row[0] ]
            chem_id = int(row[1])

            if chem_id not in self.existing_chemicals:

                # TODO handle missing columns
                record = {
                    'id': chem_id,
                    'mol_weight': float(row[6]),
                    'logp': float(row[10]),
                    'med_chem_alert': int(row[8]),
                    'is_relevant': int(row[9]),
                    'donor_count': int(row[11]),
                    'acceptor_count': int(row[12]),
                    'ring_count': int(row[13]),
                    'rot_bond_count': int(row[14]),
                    'corpus_count': int(row[7])}

                result = conn.execute(chem_ins, record)

                record = {
                    'schembl_chem_id': chem_id,
                    'smiles': row[2],
                    'std_inchi': row[3],
                    'std_inchikey': row[4],
                }

                result = conn.execute(chem_struc_ins, record)

                # TODO handle rollback of full chemical ID set
                self.existing_chemicals.add(chem_id)

            # Add the document / chemical mappings
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.TITLE,       'frequency': int(row[15]) })
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.ABSTRACT,    'frequency': int(row[16]) })
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.CLAIMS,      'frequency': int(row[17]) })
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.DESCRIPTION, 'frequency': int(row[18]) })
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.IMAGES,      'frequency': int(row[19]) })
            conn.execute(chem_map_ins, { 'schembl_doc_id': doc_id, 'schembl_chem_id': chem_id, 'field': DocumentField.ATTACHMENTS, 'frequency': int(row[20]) })


        transaction.commit()



def chunks(l, n):
    ''' Yield successive n-sized chunks from l. Via Stack Overflow.'''
    for i in xrange(0, len(l), n):
        yield l[i:i+n]

def bib_scalar(biblio, key):
    return biblio[key][0]


## Expected CSV structure:
# 0  SCPN
# 1  SureChEMBL ID
# 2  SMILES
# 3  Standard InChi
# 4  Standard InChiKey
# 5  Names
# 6  Mol Weight
# 7  Chemical Corpus Count
# 8  Med Chem Alert
# 9  Is Relevant
# 10 LogP
# 11 Donor Count
# 12 Acceptor Count
# 13 Ring Count
# 14 Rotatable Bond Count
# 15 Title field count
# 16 Abstract field count
# 17 Claims field count
# 18 Description field count
# 19 Images field count
# 20 Attachments field count