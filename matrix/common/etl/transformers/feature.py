import gzip
import os
import shutil
import typing
import urllib.request

from threading import Lock

from . import MetadataToPsvTransformer
from matrix.common.aws.redshift_handler import TableName


class FeatureTransformer(MetadataToPsvTransformer):
    """Reads gencode annotation reference and writes out rows for feature table in PSV format."""
    WRITE_LOCK = Lock()
    ANNOTATION_FTP_URL = "ftp://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_27/" \
                         "gencode.v27.chr_patch_hapl_scaff.annotation.gtf.gz"

    def __init__(self, staging_dir):
        super(FeatureTransformer, self).__init__(staging_dir)

        self.annotation_file = os.path.join(self.staging_dir, "gencode_annotation.gtf")
        self.annotation_file_gz = os.path.join(self.staging_dir, "gencode_annotation.gtf.gz")

        self._fetch_annotations()

    def _fetch_annotations(self):
        urllib.request.urlretrieve(self.ANNOTATION_FTP_URL, self.annotation_file_gz)
        with gzip.open(self.annotation_file_gz, 'rb') as f_in:
            with open(self.annotation_file, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)

    def _write_rows_to_psvs(self, *args: typing.Tuple):
        with FeatureTransformer.WRITE_LOCK:
            super(FeatureTransformer, self)._write_rows_to_psvs(*args)

    def _parse_from_metadatas(self, filename):
        features = set()

        for line in open(self.annotation_file):
            # Skip comments
            if line.startswith("#"):
                continue
            parsed = self.parse_line(line)
            if parsed:
                features.add(parsed)

        return (TableName.FEATURE, features),

    def parse_line(self, line):
        """Parse a GTF line into the fields we want."""
        p = line.strip().split("\t")
        type_ = p[2]

        if type_ not in ("gene", "transcript"):
            return ''
        chrom = p[0]
        start = p[3]
        end = p[4]
        attrs = p[8]

        id_ = ""
        name = ""
        feature_type = ""
        for attr in attrs.split(";"):
            if not attr:
                continue
            label, value = attr.strip().split(" ")
            value = eval(value)
            label = label.strip()

            if label == type_ + "_id":
                id_ = value
            elif label == type_ + "_type":
                feature_type = value
            elif label == type_ + "_name":
                name = value
        shortened_id = id_.split(".", 1)[0]
        if id_.endswith("_PAR_Y"):
            shortened_id += "_PAR_Y"

        return self._generate_psv_row(shortened_id, name, feature_type, chrom, start, end, str(type_ == "gene"))
