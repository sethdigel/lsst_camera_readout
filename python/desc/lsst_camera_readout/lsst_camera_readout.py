"""
Code to convert an eimage to individual sensor segments, applying
electronics readout effects.

 * Retrieve pixel geometry for each segment
 * Copy imaging segment pixels from eimage
 * Add dark current
 * Add defects (bright defects, dark defects, traps)
 * Apply CTE
 * Apply gain
 * Apply crosstalk
 * Add read noise and bias offset
 * Write FITS file for each amplifier

"""
from __future__ import print_function, absolute_import, division
import os
import copy
import numpy as np
import astropy.io.fits as fits
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.utils as lsstUtils
from .focalplane_readout import FocalPlaneReadout, cte_matrix

__all__ = ['ImageSource', 'set_itl_bboxes', 'set_e2v_bboxes',
           'set_phosim_bboxes']

class ImageSource(object):
    '''
    Class to create single segment images based on the pixel geometry
    described by a Camera object from lsst.afw.cameraGeom.

    Parameters
    ----------
    eimage_file : str
        Filename of the eimage FITS file from which the amplifier images
        will be extracted.
    image_array : ndarray, nominally uint16
        Two-dimensional array containing an assembled sensor image
        (only, no pre-scan or over-scan regions).  For ITL sensors the
        dimensions are (4000, 4072), i.e., with the x (serial) direction
        second.  If image_array is specified, eimage_file should be omitted.
    seg_file : str, optional
        Full path of segmentation.txt file, the PhoSim-formatted file
        that describes the properties of the sensors in the focal
        plane.  If None, then the version in obs_lsstSim/description
        will be used.
    sensor_id : str, optional
        The raft and sensor identifier, e.g., 'R22_S11'.  If None,
        then it will be extracted from the eimage_file name.
    add_read_noise : bool, optional
        Flag to add read noise.
    exptime : float, optional
        Used only if image_array is specified (so the code is unable to look
        up the EXPTIME value from the header of eimage_file).  The value of
        exptime (in seconds) defaults to 30.

    Attributes
    ----------
    eimage : astropy.io.fits.HDUList
        The input eimage data.
    eimage_data : np.array
        The data attribute of the eimage PrimaryHDU.
    _amp_images : dict
        Dictionary of amplifier images to serve as a cache so that each
        amplifier image is constructed only once.
    fp_props : FocalPlaneReadout object
        Object containing the readout properties of the sensors in the
        focal plane, extracted from the segmentation.txt file.
    '''
    def __init__(self, eimage_file=None, image_array=None, seg_file=None, 
                 sensor_id=None, add_read_noise=True, exptime=30.):
        """
        Class constructor.
        """

        if eimage_file is None:
            # If no eimage_file name is specified, create an HDUList object from
            # the (provided) image_array.  If image_array is provided, then the
            # sensor_id and exposure time also must be defined.  The code below
            # defines an HDUList data structure as if an eimage_file had been
            # read, adding the image_data and the EXPTIME keyword to the
            # primary HDU.
            self.eimage = fits.HDUList([fits.PrimaryHDU(image_array)])
            self.eimage[0].header.set('EXPTIME',exptime)
            self.eimage_data = image_array

        if image_array is None:
            self.eimage = fits.open(eimage_file)
            self.eimage_data = self.eimage[0].data

        if seg_file is None:
            seg_file = os.path.join(lsstUtils.getPackageDir('obs_lsstSim'),
                                    'description', 'segmentation.txt')
        self.fp_props = FocalPlaneReadout.read_phosim_seg_file(seg_file)

        if sensor_id is None:
            self.sensor_id = self.extract_sensor_id(eimage_file)
        else:
            self.sensor_id = sensor_id

        self._make_amp_images(add_read_noise)

    @staticmethod
    def extract_sensor_id(eimage_file):
        """
        Extract the raft and sensor ids from the eimage filename.

        Parameters
        ----------
        eimage_file : str
            Filename of the eimage FITS file from which the amplifier
            images will be extracted.

        Returns
        -------
        str
            The sensor_id, e.g., "R22_S11".
        """
        tokens = os.path.basename(eimage_file).split('_')
        return '_'.join(tokens[4:6])

    def _exptime(self):
        """
        The exposure time of the frame in seconds.

        Returns
        -------
        float
            The exposure time of the frame in seconds from the eimage_file.
        """
        return self.eimage[0].header['EXPTIME']

    def getAmpImage(self, amp_info_record, imageFactory=afwImage.ImageI):
        """
        Return an amplifier afwImage.Image object with electronics
        readout effects applied.  This method is only provided so that
        the pixel data and geometry can be displayed using
        lsst.afw.cameraGeom.utils.showAmp.

        Parameters
        ----------
        amp_info_record : lsst.afw.table.tableLib.AmpInfoRecord
            Data structure used by cameraGeom to contain the amplifier
            information such as pixel geometry, gain, noise, etc..
        imageFactory : lsst.afw.image.Image[DFIU], optional
            Image factory to be used for creating the return value.

        Returns
        -------
        lsst.afw.Image[DFIU]
            The image object containing the pixel data.
        """
        amp_name = self.amp_name(amp_info_record)
        float_image = self._amp_images[amp_name]
        if imageFactory == afwImage.ImageF:
            return float_image
        # Return image as the type given by imageFactory.  The
        # following line implicitly assumes that the bounding box for
        # the full segment matches the bounding box read from
        # segmentation.txt in the creation of the .fp_props attribute.
        output_image = imageFactory(amp_info_record.getRawBBox())
        output_image.getArray()[:] = float_image.getArray()
        return output_image

    def amp_name(self, amp_info_record):
        """
        The ampifier name derived from a lsst.afw.table.tableLib.AmpInfoRecord.

        Parameters
        ----------
        amp_info_record : lsst.afw.table.tableLib.AmpInfoRecord

        Returns
        -------
        str
             The amplifier name, e.g., "R22_S22_C00".
        """
        return '_'.join((self.sensor_id,
                         'C%s' % amp_info_record.getName()[::2]))

    def _make_amp_images(self, add_read_noise):
        """
        Make the amplifier images for all the amps in the sensor.

        Parameters
        ----------
        add_read_noise : bool
            Flag to add read noise.
        """
        self._amp_images = {}
        sensor_props = self.fp_props.get_sensor(self.sensor_id)
        for amp_name in sensor_props.amp_names:
            self._make_amp_image(amp_name)
        self._apply_crosstalk()
        for amp_name in sensor_props.amp_names:
            self._add_read_noise_and_bias(amp_name, add_read_noise)

    def _make_amp_image(self, amp_name):
        """
        Create the segment image for the amplier geometry specified in amp.

        Parameters
        ----------
        amp_name : str
            The amplifier name, e.g., "R22_S11_C00".
        """
        amp_props = self.fp_props.get_amp(amp_name)
        bbox = amp_props.mosaic_section
        full_segment = afwImage.ImageF(amp_props.full_segment)

        # Get the imaging segment (i.e., excluding prescan and
        # overscan regions), and fill with data from the eimage.
        imaging_segment = full_segment.Factory(full_segment, amp_props.imaging)
        data = self.eimage_data[bbox.getMinY():bbox.getMaxY()+1,
                                bbox.getMinX():bbox.getMaxX()+1].copy()

        # Apply flips in x and y relative to assembled eimage in order
        # to have the pixels in readout order.
        if amp_props.flip_x:
            data = data[:, ::-1]
        if amp_props.flip_y:
            data = data[::-1, :]

        imaging_segment.getArray()[:] = data
        full_arr = full_segment.getArray()

        # Add dark current.
        full_arr += np.random.poisson(amp_props.dark_current*self._exptime(),
                                      size=full_arr.shape)

        # Add defects.

        # Apply CTE.
        pcte_matrix = cte_matrix(full_arr.shape[0], amp_props.pcti)
        for col in range(0, full_arr.shape[1]):
            full_arr[:, col] = np.dot(pcte_matrix, full_arr[:, col])

        scte_matrix = cte_matrix(full_arr.shape[1], amp_props.scti)
        for row in range(0, full_arr.shape[0]):
            full_arr[row, :] = np.dot(scte_matrix, full_arr[row, :])

        # Convert to ADU.
        full_arr /= amp_props.gain

        self._amp_images[amp_name] = full_segment

    def _add_read_noise_and_bias(self, amp_name, add_read_noise):
        """
        Add read noise and bias.  This should be done as the final
        step before returning the processed image.

        Parameters
        ----------
        amp_name : str
            The amplifier name, e.g., "R22_S11_C00".
        add_read_noise : bool
            Flag to add read noise.  Should be set to False for GalSim images.
        """
        amp_props = self.fp_props.get_amp(amp_name)
        full_arr = self._amp_images[amp_name].getArray()
        if add_read_noise:
            full_arr += np.random.normal(scale=amp_props.read_noise,
                                         size=full_arr.shape)
        full_arr += amp_props.bias_level

    def _apply_crosstalk(self):
        """
        Apply inter-amplifier crosstalk using the cross-talk matrix
        from segmentation.txt.  This should be run only once and
        only after ._make_amp_image has been run for each amplifier.
        """
        sensor_props = self.fp_props.get_sensor(self.sensor_id)
        imarrs = np.array([self._amp_images[amp_name].getArray()
                           for amp_name in sensor_props.amp_names])
        for amp_name in sensor_props.amp_names:
            amp_props = self.fp_props.get_amp(amp_name)
            self._amp_images[amp_name].getArray()[:, :] \
                = sum([x*y for x, y in zip(imarrs, amp_props.crosstalk)])

    def write_amplifier_image(self, amp_name, outfile, clobber=True):
        """
        Write the pixel data for the specified amplifier as FITS image.

        Parameters
        ----------
        amp_name : str
            Amplifier id, e.g., "R22_S11_C00".
        outfile : str
            Filename of the FITS file to be written.
        clobber : bool, optional
            Flag whether to overwrite an existing output file.
        """
        output = fits.HDUList()
        output.append(copy.deepcopy(self.eimage[0]))
        amp_image = self._amp_images[amp_name]
        amp_props = self.fp_props.get_amp(amp_name)
        output[0].data = amp_image.getArray()
        output[0].header['DATASEC'] \
            = self._noao_section_keyword(amp_props.imaging)
        output[0].header['DETSEC'] \
            = self._noao_section_keyword(amp_props.mosaic_section,
                                         flipx=amp_props.flip_x,
                                         flipy=amp_props.flip_y)
        output[0].header['BIASSEC'] \
            = self._noao_section_keyword(amp_props.serial_overscan)
        output[0].header['GAIN'] = amp_props.gain
        output.writeto(outfile, clobber=clobber)

    @staticmethod
    def _noao_section_keyword(bbox, flipx=False, flipy=False):
        """
        Convert bounding boxes into NOAO section keywords.

        Parameters
        ----------
        bbox : lsst.afw.geom.Box2I
            Bounding box.
        flipx : bool
            Flag to indicate that data should be flipped in the x-direction.
        flipy : bool
            Flag to indicate that data should be flipped in the y-direction.
        """
        xmin, xmax = bbox.getMinX()+1, bbox.getMaxX()+1
        ymin, ymax = bbox.getMinY()+1, bbox.getMaxY()+1
        if flipx:
            xmin, xmax = xmax, xmin
        if flipy:
            ymin, ymax = ymax, ymin
        return '[%i:%i,%i:%i]' % (xmin, xmax, ymin, ymax)

def set_itl_bboxes(amp):
    """
    Function to apply realistic pixel geometry for ITL sensors.

    Parameters
    ----------
    amp : lsst.afw.table.tableLib.AmpInfoRecord
        Data structure containing the amplifier information such as
        pixel geometry, gain, noise, etc..

    Returns
    -------
    lsst.afw.table.tableLib.AmpInfoRecord
        The updated AmpInfoRecord.
    """
    amp.setRawBBox(afwGeom.Box2I(afwGeom.Point2I(0, 0),
                                 afwGeom.Extent2I(532, 2020)))
    amp.setRawDataBBox(afwGeom.Box2I(afwGeom.Point2I(3, 0),
                                     afwGeom.Extent2I(509, 2000)))
    amp.setRawHorizontalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(512, 0),
                                                   afwGeom.Extent2I(20, 2000)))
    amp.setRawVerticalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 2000),
                                                 afwGeom.Extent2I(532, 20)))
    amp.setRawPrescanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 0),
                                        afwGeom.Extent2I(3, 2000)))
    return amp

def set_e2v_bboxes(amp):
    """
    Function to apply realistic pixel geometry for e2v sensors.

    Parameters
    ----------
    amp : lsst.afw.table.tableLib.AmpInfoRecord
        Data structure containing the amplifier information such as
        pixel geometry, gain, noise, etc..

    Returns
    -------
    lsst.afw.table.tableLib.AmpInfoRecord
        The updated AmpInfoRecord.
    """
    amp.setRawBBox(afwGeom.Box2I(afwGeom.Point2I(0, 0),
                                 afwGeom.Extent2I(542, 2022)))
    amp.setRawDataBBox(afwGeom.Box2I(afwGeom.Point2I(10, 0),
                                     afwGeom.Extent2I(522, 2002)))
    amp.setRawHorizontalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(522, 0),
                                                   afwGeom.Extent2I(20, 2002)))
    amp.setRawVerticalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 2002),
                                                 afwGeom.Extent2I(542, 20)))
    amp.setRawPrescanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 0),
                                        afwGeom.Extent2I(10, 2002)))
    return amp

def set_phosim_bboxes(amp):
    """
    Function to apply the segmentation.txt geometry.

    Parameters
    ----------
    amp : lsst.afw.table.tableLib.AmpInfoRecord
        Data structure containing the amplifier information such as
        pixel geometry, gain, noise, etc..

    Returns
    -------
    lsst.afw.table.tableLib.AmpInfoRecord
        The updated AmpInfoRecord.
    """
    amp.setRawBBox(afwGeom.Box2I(afwGeom.Point2I(0, 0),
                                 afwGeom.Extent2I(519, 2001)))
    amp.setRawDataBBox(afwGeom.Box2I(afwGeom.Point2I(4, 1),
                                     afwGeom.Extent2I(509, 2000)))
    amp.setRawHorizontalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(513, 1),
                                                   afwGeom.Extent2I(6, 2000)))
    amp.setRawVerticalOverscanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 2001),
                                                 afwGeom.Extent2I(519, 0)))
    amp.setRawPrescanBBox(afwGeom.Box2I(afwGeom.Point2I(0, 1),
                                        afwGeom.Extent2I(4, 2000)))
    return amp
