import builtins
import hashlib

import numpy

from ..server.object_cache import with_object_cache
from ..structures.array import (
    ArrayMacroStructure,
    MachineDataType,
)


def subdirectory_handler(path):
    """
    Sniff a subdirectory for TIFF sequences.
    """
    # Max fraction of files that do not look like TIFF sequence files
    RELATIVE_THRESHOLD = 0.2
    # Max number of files that do not look like TIFF sequence files
    ABSOLUTE_THRESHOLD = 10
    filepaths = []
    outliers = 0
    for filepath in path.iterdir():
        if not filepath.is_file():
            # Skip directories.
            continue
        if filepath.name.startswith("."):
            # Skip hidden files.
            continue
        if (filepath.suffix in (".tif", ".tiff")) and filepath.stem[-3:].isdigit():
            # This looks like something123.tif.
            filepaths.append(filepath)
            continue
        outliers += 1

    fraction = outliers / (outliers + len(filepaths))
    if (outliers <= ABSOLUTE_THRESHOLD) and (fraction <= RELATIVE_THRESHOLD):
        # This looks like a TIFF sequence directory.
        import tifffile

        seq = tifffile.TiffSequence(sorted(filepaths))
        return TiffSequenceReader(seq)

    return None


class TiffSequenceReader:

    structure_family = "array"

    def __init__(self, seq):
        self._seq = seq
        self._metadata = {}
        self._cache_key = (
            type(self).__module__,
            type(self).__qualname__,
            hashlib.md5(str(seq.files).encode()).hexdigest(),
        )

    @property
    def metadata(self):
        return self._metadata

    def read(self, slice=None):
        """Return a numpy array

        Receives a sequence of values to select from a collection of tiff files that were saved in a folder
        The input order is defined as files --> X slice --> Y slice
        read() can receive one value or one slice to select all the data from one file or a sequence of files;
        or it can receive a tuple of up to three values (int or slice) to select a more specific sequence of pixels
        of a group of images
        """

        # Print("Inside Reader:", slice)
        if slice is None:
            return with_object_cache(self._cache_key, _safe_asarray, self._seq)
        if isinstance(slice, int):
            # e.g. read(slice=0)
            return with_object_cache(
                self._cache_key + (slice,), _safe_asarray, self._seq, index=slice
            )
        # e.g. read(slice=(...))
        if isinstance(slice, tuple):
            image_axis, *the_rest = slice
            # Could be int or slice
            # (0, slice(...)) or (0,....) are converted to a list
            if isinstance(image_axis, int):
                # e.g. read(slice=(0, ....))
                arr = with_object_cache(
                    self._cache_key + (image_axis,), _safe_asarray, self._seq, index=image_axis
                )
            if isinstance(image_axis, builtins.slice):
                if image_axis.start is None:
                    slice_start = 0
                else:
                    slice_start = image_axis.start
                if image_axis.step is None:
                    slice_step = 1
                else:
                    slice_step = image_axis.step
                arr = numpy.stack(
                    [
                        with_object_cache(
                            self._cache_key + (i,), _safe_asarray, self._seq, index=i
                        )
                        for i in range(slice_start, image_axis.stop, slice_step)
                    ]
                )
            arr = arr[tuple(the_rest)]
            return arr
        if isinstance(slice, builtins.slice):
            # Check for start and step which can be optional
            if slice.start is None:
                slice_start = 0
            else:
                slice_start = slice.start
            if slice.step is None:
                slice_step = 1
            else:
                slice_step = slice.step
            arr = numpy.stack(
                [
                    with_object_cache(self._cache_key + (i,), _safe_asarray, self._seq, index=i)
                    for i in range(slice_start, slice.stop, slice_step)
                ]
            )
            return arr

    def read_block(self, block, slice=None):
        # Print("Block:", block)
        if block[1:] != (0, 0):
            raise IndexError(block)
        arr = self.read(builtins.slice(block[0], block[0] + 1))
        if slice is not None:
            arr = arr[slice]
        return arr

    def microstructure(self):
        # Assume all files have the same data type
        return MachineDataType.from_numpy_dtype(self.read(slice=0).dtype)

    def macrostructure(self):
        shape = (len(self._seq), *self.read(slice=0).shape)
        # print("array shape", shape)
        return ArrayMacroStructure(
            shape=shape,
            # one chunks per underlying TIFF file
            chunks=(
                (1,) * shape[0],
                (shape[1],),
                (shape[2],),
            ),
        )


def _safe_asarray(seq, index=None):
    """
    Wrap FileSequence.asarray to cope with this breaking change in tifffile.

    https://github.com/cgohlke/tifffile/commit/18315846ebf0b31126dfdeffee8e0ef30d81b31c#diff-d8d5cccae6533b861bdf6c0c20c6f49415978effec9a13797909535f3fc7390dL9563-R9746  # noqa
    """

    try:
        return seq.asarray(index=index)
    except TypeError:
        # usage for versions before 2021.10.10
        return seq.asarray(file=index)
