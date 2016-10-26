#! /usr/bin/env python
# -*- coding: utf-8 -*-


import warnings
import numpy as np
import cPickle
from copy import deepcopy
from collections import OrderedDict as odict
from itertools import izip

import sncosmo
from sncosmo.photdata                 import standardize_data, dict_to_array
from astropy.table                    import Table, vstack
from astropy.utils.console            import ProgressBar

from astrobject                       import BaseObject
from astrobject.utils.tools           import kwargs_update
from astrobject.utils.plot.skybins    import SurveyField, SurveyFieldBins 

_d2r = np.pi/180

__all__ = ["SimulSurvey", "SurveyPlan", "LightcurveCollection"] # to be changed

#######################################
#                                     #
# Survey: Simulation Base             #
#                                     #
#######################################
class SimulSurvey( BaseObject ):
    """
    Basic survey object

    Collects transient generator, survey plan and instrument properties.
    Generates lightcurves for the observed transients.

    See the included notebooks for usage examples.
    """
    __nature__ = "SimulSurvey"
    
    PROPERTIES         = ["generator","instruments","plan"]
    SIDE_PROPERTIES    = ["cadence","blinded_bias"]
    DERIVED_PROPERTIES = ["obs_fields", "non_field_obs", "non_field_obs_exist"]

    def __init__(self,generator=None, plan=None,
                 instprop=None, blinded_bias=None,
                 empty=False):
        """
        Parameters:
        ----------
        generator: [TransientGenerator or derived child like SNIaGenerator]
            Transient generator object that collects the sncosmo.Model
            parameters

        plan: [SurveyPlan object]
            Survey plan containing the pointings
        
        instprop: [dict]
            Dictionary containing details of the instruments used in the survey;
            see SimulSurvey.set_instruments() for details

        blinded_bias: [dict]
            Dictionary of band names and floats. A blinded offset for each band will
            randomly be drawn from a uniform distribution limited by the floats. 
        """
        self.__build__()
        if empty:
            return

        self.create(generator, plan, instprop, blinded_bias)

    def create(self, generator, plan, instprop, blinded_bias):
        """
        """
        if generator is not None:
            self.set_target_generator(generator)

        if plan is not None:
            self.set_plan(plan)

        if instprop is not None:
            self.set_instruments(instprop)

        if blinded_bias is not None:
            self.set_blinded_bias(blinded_bias)

    # =========================== #
    # = Main Methods            = #
    # =========================== #

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #
    def get_lightcurves(self, progress_bar=False, notebook=False):
        """Simulate the lightcurves. Requires all basic components
        (generator, plan and instruments) to be set.

        Options:
        --------
        progress_bar: [bool]
            Show an astropy ProgressBar during the process

        notebook: [bool]
            Value is passed the ProgressBar's option 'ipython_widget';
            only works if progress_bar is True; use True in jupyter
            notebooks and False in a shell. May not work properly depending
            in package versions.

        Return
        ------
        LightcurveCollection object
        """
        if not self.is_set():
            raise AttributeError("plan, generator or instrument not set")

        lcs = LightcurveCollection(empty=True)
        gen = izip(self.generator.get_lightcurve_full_param(),
                   self._get_observations_())
        if progress_bar:
            self._assign_obs_fields_(progress_bar=True, notebook=notebook)
            self._assign_non_field_obs_(progress_bar=True, notebook=notebook)
            
            print 'Generating lightcurves'
            with ProgressBar(self.generator.ntransient,
                             ipython_widget=notebook) as bar:
                for k, (p, obs) in enumerate(gen):
                    if obs is not None:
                        lcs.add(self._get_lightcurve_(p, obs, k))
                    bar.update()
        else:
            for k, (p, obs) in enumerate(gen):
                if obs is not None:
                    lcs.add(self._get_lightcurve_(p, obs, k))

        return lcs
            
    def _get_lightcurve_(self, p, obs, idx_orig=None):
        """
        """        
        if obs is not None:
            ra, dec, mwebv_sfd98 = p.pop('ra'), p.pop('dec'), p.pop('mwebv_sfd98')

            # Get unperturbed lc from sncosmo
            lc = sncosmo.realize_lcs(obs, self.generator.model, [p],
                                     scatter=False)[0]

            # Replace fluxerrors with covariance matrix that contains
            # correlated terms for the calibration uncertainty
            fluxerr = np.sqrt(obs['skynoise']**2 +
                              np.abs(lc['flux']) / obs['gain'])
            
            fluxcov = np.diag(fluxerr**2)
            save_cov = False
            for band in set(obs['band']):
                if self.instruments[band]['err_calib'] is not None:
                    save_cov = True
                    idx = np.where(obs['band'] == band)[0]
                    err = self.instruments[band]['err_calib']
                    for k0 in idx:
                        for k1 in idx:
                            fluxcov[k0,k1] += (lc['flux'][k0] * 
                                               lc['flux'][k1] *
                                               err**2)

            # Add random (but correlated) noise to the fluxes
            fluxchol = np.linalg.cholesky(fluxcov)
            flux = lc['flux'] + fluxchol.dot(np.random.randn(len(lc)))

            # Apply blinded bias if given
            if self.blinded_bias is not None:
                bias_array = np.array([self.blinded_bias[band]
                                       if band in self.blinded_bias.keys() else 0
                                       for band in obs['band']])
                flux *= 10 ** (-0.4*bias_array)

            lc['flux'] = flux
            lc['fluxerr'] = np.sqrt(np.diag(fluxcov))

            # Additional metadata for the lc fitter
            lc.meta['ra'] = ra
            lc.meta['dec'] = dec
            if save_cov:
                lc.meta['fluxcov'] = fluxcov
            lc.meta['mwebv_sfd98'] = mwebv_sfd98
            if idx_orig is not None:
                lc.meta['idx_orig'] = idx_orig
        else:
            lc = None

        return lc

    # ---------------------- #
    # - Setter Methods     - #
    # ---------------------- #

    # -------------
    # - Targets
    def set_target_generator(self, generator):
        """Set or replace the generator for transient properties 

        Parameters
        ----------
        generator: [TransientGenerator or derived child like SNIaGenerator]
            Transient generator object that collects the sncosmo.Model
            parameters
        """
        if "__nature__" not in dir(generator) or\
          generator.__nature__ != "TransientGenerator":
            raise TypeError("generator must be an astrobject TransientGenerator")

        if not generator.has_lightcurves():
            warnings.warn("No lightcurves set in the given transient generator")

        self._properties["generator"] = generator

    # -------------
    # - SurveyPlan
    def set_plan(self,plan):
        """Set or replace survey plan 

        Parameters
        ----------
        plan: [SurveyPlan object]
            Survey plan containing the pointings
        """
        # ----------------------
        # - Load cadence here
        if "__nature__" not in dir(plan) or \
          plan.__nature__ != "SurveyPlan":
            raise TypeError("the input 'plan' must be an astrobject SurveyPlan")
        self._properties["plan"] = plan

        # ----------------------------
        # - Set back the observations
        # self._reset_observations_()
        self._reset_obs_fields_()
        self._reset_non_field_obs_()

    # -------------
    # - Instruments
    def set_instruments(self,properties):
        """Set or replace instrument properties
        properties must be a dictionary containing the
        
        Parameters
        ----------
        properties: [dict]
            Dictionary containing details of the instruments used in the survey;
            instruments' information (bandname,gain,zp,zpsys,err_calib) related
            to each band. (Note that the band names must be registered in sncosmo
            and stand for a combination of instrument and bandpass. Therefore you
            must register a copy of  a bandpass that you want to use twice.)

        example..
        ---------
        properties = {"desg":{"gain":1,"zp":30,"zpsys":'ab',"err_calib":0.005},
                      "desr":{"gain":1,"zp":30,"zpsys":'ab',"err_calib":0.005}}
        """
        prop = deepcopy(properties)
        for band,d in prop.items():
            gain,zp,zpsys = d.pop("gain"),d.pop("zp"),d.pop("zpsys","ab")
            err_calib = d.pop("err_calib", None)
            if gain is None or zp is None:
                raise ValueError('gain or zp is None or not defined for %s'%band)
            self.add_instrument(band,gain,zp,zpsys,err_calib,
                                update=False,**d)

        #self._reset_observations_()

    # -----------------------
    # - Blinded bias in bands
    def set_blinded_bias(self, bias):
        """Set or reset blinded bias

        Parameters
        ----------
        bias: [dict]
            Dictionary of band names and floats. A blinded offset for each band will
            randomly be drawn from a uniform distribution limited by the floats. 
        """
        self._side_properties['blinded_bias'] = {k: np.random.uniform(-v, v) 
                                                 for k, v in bias.items()}

    # ---------------------- #
    # - Add Stuffs         - #
    # ---------------------- #
    def add_instrument(self,bandname,gain,zp,zpsys="ab",err_calib=None,
                       force_it=True,update=True,**kwargs):
        """Add a single instrument. See SimulSurvey.add_instruments() for more
        details.        
        """
        if self.instruments is None:
            self._properties["instruments"] = {}

        if bandname in self.instruments.keys() and not force_it:
            raise AttributeError("%s is already defined."+\
                                 " Set force_it to True to overwrite it. ")

        instprop = {"gain":gain,"zp":zp,"zpsys":zpsys,"err_calib":err_calib}
        self.instruments[bandname] = kwargs_update(instprop,**kwargs)

        if update:
            # self._reset_observations_()
            pass

    # ---------------------- #
    # - Recover Methods    - #
    # ---------------------- #
    #def recover_targets(self):
    #    """
    #    bunch threshold...
    #    """
    #
    #def recover_lightcurves(self):
    #    """
    #    """

    # =========================== #
    # = Internal Methods        = #
    # =========================== #
    def _update_lc_(self):
        """
        """
        # -----------------------------
        # -- Do you have all you need ?
        if not self.is_set():
            return
            
    def _get_observations_(self):
        """
        """
        # -------------
        # - Input test
        if self.plan is None or self.instruments is None:
            raise AttributeError("Plan or Instruments is not set.")

        # -----------------------
        # - Check if instruments exists
        all_instruments = np.unique(self.cadence["band"])
        if not np.all([i in self.instruments.keys() for i in all_instruments]):
            raise ValueError("Some of the instrument in cadence have not been defined."+"\n"+
                             "given instruments :"+", ".join(all_instruments.tolist())+"\n"+
                             "known instruments :"+", ".join(self.instruments.keys()))

        # -----------------------
        # - Based on the model get a reasonable time scale for each transient
        mjd = self.generator.mjd
        z = np.array(self.generator.zcmb)
        mjd_range = [mjd + self.generator.model.mintime() * (1 + z), 
                     mjd + self.generator.model.maxtime() * (1 + z)]

        # -----------------------
        # - Let's build the tables
        for f, n, d0, d1 in zip(self.obs_fields, self.non_field_obs,
                                mjd_range[0], mjd_range[1]):
            obs = self.plan.observed_on(f, n, (d0, d1))
            if len(obs) > 0: 
                yield Table(
                    {"time": obs["time"],
                     "band": obs["band"],
                     "skynoise": obs["skynoise"],
                     "gain":[self.instruments[b]["gain"] for b in obs["band"]],
                     "zp":[self.instruments[b]["zp"] for b in obs["band"]],
                     "zpsys":[self.instruments[b]["zpsys"] for b in obs["band"]]}
                )
            else:
                yield None

    def _assign_obs_fields_(self, progress_bar=False, notebook=False):
        """
        """
        self._derived_properties["obs_fields"] = self.plan.get_obs_fields(
            self.generator.ra,
            self.generator.dec,
            progress_bar=progress_bar,
            notebook=notebook
        )

    def _reset_obs_fields_(self):
        """
        """
        self._derived_properties["obs_fields"] = None

    def _assign_non_field_obs_(self, progress_bar=False, notebook=False):
        """
        """
        self._derived_properties["non_field_obs"] = self.plan.get_non_field_obs(
            self.generator.ra,
            self.generator.dec,
            progress_bar=progress_bar,
            notebook=notebook
        )

    def _reset_non_field_obs_(self):
        """
        """
        self._derived_properties["non_field_obs"] = None
        self._derived_properties["non_field_obs_exist"] = None
    
    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def instruments(self):
        """The basic information relative to the instrument used for the survey"""
        return self._properties["instruments"]

    @property
    def generator(self):
        """The instance that enable to create fake targets"""
        return self._properties["generator"]

    @property
    def plan(self):
        """This is the survey plan including field definitions and telescope pointings"""
        return self._properties["plan"]

    def is_set(self):
        """This parameter is True if this has cadence, instruments and genetor set"""
        return not (self.instruments is None or \
                    self.generator is None or \
                    self.plan is None)

    # ------------------
    # - Side properties
    @property
    def cadence(self):
        """This is a table containing where the telescope is pointed with which band."""
        if self._properties["plan"] is not None:
            return self._properties["plan"].cadence
        else:
            raise ValueError("Property 'plan' not set yet")

    @property
    def blinded_bias(self):
        """Blinded bias applied to specific bands for all observations"""
        return self._side_properties["blinded_bias"]

    # ------------------
    # - Derived values
    @property
    def obs_fields(self):
        """Transients are assigned fields that they are found"""
        if self._derived_properties["obs_fields"] is None:
            self._assign_obs_fields_()

        return self._derived_properties["obs_fields"]

    @property
    def non_field_obs(self):
        """If the plan contains pointings with field id, prepare a list of those."""
        if (self._derived_properties["non_field_obs"] is None
            and self.non_field_obs_exist is False):
            self._assign_non_field_obs_()
            
        if self._derived_properties["non_field_obs"] is None:
            self._derived_properties["non_field_obs_exist"] = False
        else:
            self._derived_properties["non_field_obs_exist"] = True

        if self.non_field_obs_exist is False:
            return [None for k in xrange(self.generator.ntransient)]
        return self._derived_properties["non_field_obs"]

    @property
    def non_field_obs_exist(self):
        """Avoid checking for non-field pointings more than once."""
        return self._derived_properties["non_field_obs_exist"]

#######################################
#                                     #
# Survey: Plan object                 #
#                                     #
#######################################
class SurveyPlan( BaseObject ):
    """
    Survey Plan
    contains the list of observation times, bands and pointings and
    can return that times and bands, which a transient is observed at/with.
    A list of fields can be given to simplify adding observations and avoid 
    lookups whether an object is in a certain field.
    Currently assumes a single instrument, especially for FoV width and height.
    """
    __nature__ = "SurveyPlan"

    PROPERTIES         = ["cadence", "width", "height"]
    SIDE_PROPERTIES    = ["fields"]
    DERIVED_PROPERTIES = []

    def __init__(self, time=None, ra=None, dec=None, band=None, skynoise=None, 
                 obs_field=None, width=6.86, height=6.86, fields=None, empty=False,
                 load_opsim=None, **kwargs):
        """
        Parameters:
        ----------
        time: [array-like object of floats]
            Array of observation times

        ra, dec: [array-like objects of floats]
            Arrays of pointing coordinates (not required if using obs_field)

        band: [list of strings]
            Band passes used for each pointing
        
        skynoise: [array-like object of floats]
            Array of skynoise values

        obs_field: [array-like object of ints]
            Array of fieldIDs (requires fields to be set as well)

        width, height: [floats]
            Dimensions of the observing fields

        fields: [dict]
            Arguments to be passed to
            astrobject.utils.plot.skybins.SurveyFieldBins
            (see examples)

        load_opsim: [str]
            Filename of the opsim sqlite file containing the plan
            (if not None, everything except width, height and fields
            will be ignored)

        **kwargs are passed to SurveyPlan.load_opsim()
        """
        self.__build__()
        if empty:
            return

        self.create(time=time,ra=ra,dec=dec,band=band,skynoise=skynoise,
                    obs_field=obs_field,fields=fields, load_opsim=load_opsim,
                    **kwargs)

    def create(self, time=None, ra=None, dec=None, band=None, skynoise=None, 
               obs_field=None, width=6.86, height=6.86, fields=None, 
               load_opsim=None, **kwargs):
        """
        """
        self._properties["width"] = float(width)
        self._properties["height"] = float(height)

        if fields is not None:
            self.set_fields(**fields)

        if load_opsim is None:
            self.add_observation(time,band,skynoise,ra=ra,dec=dec,field=obs_field)
        else:
            self.load_opsim(load_opsim, **kwargs)

    # =========================== #
    # = Main Methods            = #
    # =========================== #

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #

    # ---------------------- #
    # - Setter Methods     - #
    # ---------------------- #
    def set_fields(self, ra=None, dec=None, **kwargs):
        """
        """
        kwargs["width"] = kwargs.get("width", self.width)
        kwargs["height"] = kwargs.get("height", self.height)
        
        self._side_properties["fields"] = SurveyFieldBins(ra, dec, **kwargs)

        if self.cadence is not None and np.any(np.isnan(self.cadence['field'])):
            warnings.warning("cadence was already set, field pointing will be updated")
            self._update_field_radec()

    def add_observation(self, time, band, skynoise, ra=None, dec=None, field=None):
        """
        """
        if ra is None and dec is None and field is None:
            raise ValueError("Either field or ra and dec must to specified.")
        elif ra is None and dec is None:
            if self.fields is None:
                raise ValueError("Survey fields not defined.")
            else:
                idx = self.fields.field_id_index[field]
                ra = self.fields.ra[idx]
                dec = self.fields.dec[idx]
        elif field is None:
            field = np.array([np.nan for r in ra])

        new_obs = Table({"time": time,
                         "band": band,
                         "skynoise": skynoise,
                         "RA": ra,
                         "Dec": dec,
                         "field": field})

        if self._properties["cadence"] is None:
            self._properties["cadence"] = new_obs
        else:
            self._properties["cadence"] = vstack((self._properties["cadence"], 
                                                  new_obs))

    # ---------------------- #
    # - Load Method        - #
    # ---------------------- #
    def load_opsim(self, filename, table_name="Summary", band_dict=None,
                   default_depth=21., zp=30.):
        """Load plan from opsim sqlite file;
        see https://confluence.lsstcorp.org/display/SIM/Summary+Table+Column+Descriptions
        for format description

        Currently only the used columns are loaded

        Parameters
        ----------
        table_name: [str]
            Name of table in SQLite DB 

        band_dict: [dict]
            Dictionary for converting filter names

        default_depth: [float]
            Default value for 5-sigma depth if column in file is empty 
        
        zp: [float]
            Zero point for converting sky brightness from mag to flux units
            (should match the zp used in instprop for SimulSurvey)
        """        
        import sqlite3
        connection = sqlite3.connect(filename)

        # define columns name and keys to be fetched
        to_fetch = odict()
        to_fetch['time'] = 'expMJD'
        to_fetch['band_raw'] = 'filter' # Currently is a float not a string in Eric's example
        #to_fetch['filtskybrightness'] = 'filtSkyBrightness' # in mag/arcsec^2 
        #to_fetch['seeing'] = 'finSeeing' # effective FWHM used to calculate skynoise
        to_fetch['ra'] = 'fieldRA'
        to_fetch['dec'] = 'fieldDec'
        to_fetch['field'] = 'fieldID'
        to_fetch['depth'] = 'fiveSigmaDepth'
        
        loaded = odict()
        for key, value in to_fetch.items():
            # This is not safe against injection (but should be OK)
            # TODO: Add function to sanitize input
            cmd = 'SELECT %s from %s;'%(value, table_name)
            tmp = connection.execute(cmd)
            loaded[key] = np.array([a[0] for a in tmp])

        connection.close()

        loaded['ra'] /= _d2r
        loaded['dec'] /= _d2r

        loaded['depth'] = np.array([(d if d is not None else default_depth)
                                    for d in loaded['depth']])
        
        loaded['skynoise'] = 10 ** (-0.4 * (loaded['depth']-zp)) / 5
        
        if band_dict is not None:
            loaded['band'] = [band_dict[band] for band in loaded['band_raw']]
        else:
            loaded['band'] = loaded['band_raw']
 
        self.add_observation(loaded['time'],loaded['band'],loaded['skynoise'],
                             ra=loaded['ra'],dec=loaded['dec'],
                             field=loaded['field'])

    # ================================== #
    # = Observation time determination = #
    # ================================== #
    def get_obs_fields(self, ra, dec, progress_bar=False, notebook=False):
        """
        """
        if (self.fields is not None and 
            not np.all(np.isnan(self.cadence["field"]))):
            return self.fields.coord2field(ra, dec, progress_bar=progress_bar,
                                           notebook=notebook)
        else:
            return None
        
    def get_non_field_obs(self, ra, dec, progress_bar=False, notebook=False):
        """
        """
        observed = False
        gen = self.cadence[np.isnan(self.cadence["field"])]
        
        if progress_bar and len(gen) > 0:
            print "Finding transients observed in custom pointings"
            gen = ProgressBar(gen, ipython_widget=notebook)

        for k, obs in enumerate(gen):
            tmp_f = SurveyField(obs["RA"], obs["Dec"], 
                                self.width, self.height)
            b = tmp_f.coord_in_field(ra, dec)

            # Setup output as dictionaries that can be converted to Tables and
            # sorted later
            if k == 0:
                if type(b) is np.bool_:
                    single_coord = True
                    out = np.array([], dtype=int)
                else:
                    out = [np.array([], dtype=int) for r in ra]

            if single_coord:
                if b:
                    observed = True
                    out = np.append(out, [k])
            else:
                for l in np.where(b)[0]:
                    observed = True
                    out[l] = np.append(out[l], [k])

        if observed:
            return out
        else:
            return None

    def observed_on(self, fields=None, non_field=None, mjd_range=None):
        """
        mjd_range must be 2-tuple
        fields and non_field np.arrays
        """
        if fields is None and non_field is None:
            raise ValueError("Provide arrays of fields and/or other pointings") 

        out = {'time': [], 'band': [], 'skynoise': []}
        if fields is not None:
            for l in fields:
                mask = (self.cadence['field'] == l)
                out['time'].extend(self.cadence['time'][mask].quantity.value)
                out['band'].extend(self.cadence['band'][mask])
                out['skynoise'].extend(self.cadence['skynoise']
                                       [mask].quantity.value)

        if non_field is not None:
            mask = np.isnan(self.cadence["field"])
            out['time'].extend(self.cadence['time'][mask][non_field].quantity.value)
            out['band'].extend(self.cadence['band'][mask][non_field])
            out['skynoise'].extend(self.cadence['skynoise']
                                   [mask][non_field].quantity.value)

        
        table = Table(out, meta={})
        idx = np.argsort(table['time'])
        if mjd_range is None:
            return table[idx]
        else:
            t = table[idx]
            return t[(t['time'] >= mjd_range[0]) &
                     (t['time'] <= mjd_range[1])]

    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def cadence(self):
        """Table of observations"""
        return self._properties["cadence"]

    @property
    def width(self):
        """field width"""
        return self._properties["width"]

    @property
    def height(self):
        """field height"""
        return self._properties["height"]

    # ------------------
    # - Side properties                    
    @property
    def fields(self):
        """Observation fields"""
        return self._side_properties["fields"]

#######################################
#                                     #
# Survey: Plan object                 #
#                                     #
#######################################
class LightcurveCollection( BaseObject ):
    """
    LightcurveCollection
    Collects and organizes lightcurves (e.g. simulated by a Survey object)
    for easy access and serialization while try to avoid excessive memory
    use by Astropy Tables. Superficially acts like a list of tables but
    creates them on the fly from structured numpy arrays
    """
    __nature__ = "LightcurveCollection"

    PROPERTIES         = ['lcs','meta']
    SIDE_PROPERTIES    = []
    DERIVED_PROPERTIES = []

    def __init__(self, lcs=None, empty=False, load=None):
        """
        Parameters:
        ----------
        TBA

        """
        self.__build__()
        if empty:
            return

        self.create(lcs=lcs, load=load)

    def create(self, lcs=None, load=None):
        """
        """
        if load is None:
            self.add(lcs)
        else:
            self.load(load)

    # =========================== #
    # = Main Methods            = #
    # =========================== #
    def add(self, lcs):
        """
        """
        if type(lcs) is list:
            meta = [lc.meta for lc in lcs]
        else:
            meta = lcs.meta

        self._add_lcs_(lcs)
        self._add_meta_(meta)    

    def load(self, filename):
        """
        """
        loaded = cPickle.load(open(filename))
        self._properties['lcs'] = loaded['lcs']
        self._properties['meta'] = loaded['meta']

    def save(self, filename):
        """
        """
        cPickle.dump({'lcs': self._properties["lcs"],
                      'meta': self._properties["meta"]},
                     open(filename, 'w'))

    # ---------------------- #
    # - Get Methods        - #
    # ---------------------- #
    def __getitem__(self, given):
        """
        """
        if isinstance(given, slice):
            return [Table(data=data,
                          meta={k: v for k, v in zip(meta.dtype.names, meta)})
                    for data, meta in
                    zip(self.lcs[given], self.meta[given])]
        else:
            meta = self.meta[given]
            return Table(data=self.lcs[given],
                         meta={k: v for k, v in zip(meta.dtype.names, meta)}) 
            
    # ---------------------- #
    # - Add Methods        - #
    # ---------------------- #

    def _add_lcs_(self, lcs):
        """
        """
        if self.lcs is None:
            self._properties['lcs'] = []

        if type(lcs) is list:
            for lc in lcs:
                self._properties['lcs'].append(standardize_data(lc))
        else:
            self._properties['lcs'].append(standardize_data(lcs))

    def _add_meta_(self, meta):
        """
        """
        if type(meta) is list:
            if self.meta is None:
                keys = [k for k in meta[0].keys()]
                dtypes = [type(v) for v in meta[0].values()]
                self._create_meta_(keys, dtypes)
                
            for meta_ in meta:
                for k in self.meta.dtype.names:
                    self._properties['meta'][k] = np.append(
                        self._properties['meta'][k],
                        meta_[k]
                    )
        else:
            if self.meta is None:
                keys = [k for k in meta.keys()]
                dtypes = [type(v) for v in meta.values()]
                self._create_meta_(keys, dtypes)
                
            for k in self.meta.dtype.names:
                self._properties['meta'][k] = np.append(
                    self._properties['meta'][k],
                    meta[k]
                )            

    def _create_meta_(self, keys, dtypes):
        """
        Create the ordered ditcionary of meta parameters based of first item
        """
        self._properties['meta'] = odict()
        for k, t in zip(keys, dtypes):
            self._properties['meta'][k] = np.array([], dtype=t)
                
    # =========================== #
    # = Properties and Settings = #
    # =========================== #
    @property
    def lcs(self):
        """List of lcs as numpy structured arrays without meta parameters"""
        return self._properties["lcs"]

    @property
    def meta(self):
        """numpy structured array with of meta parameters"""
        if self._properties["meta"] is None:
            return None
        return dict_to_array(self._properties["meta"])

    