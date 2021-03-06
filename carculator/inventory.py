from . import DATA_DIR
import sys
import glob
from .background_systems import BackgroundSystemModel
from .export import ExportInventory
from inspect import currentframe, getframeinfo
from pathlib import Path
from scipy import sparse
import csv
import itertools
import numexpr as ne
import numpy as np
import xarray as xr
import pandas as pd
import itertools

REMIND_FILES_DIR = DATA_DIR / "IAM"


class InventoryCalculation:
    """
    Build and solve the inventory for results characterization and inventory export

    Vehicles to be analyzed can be filtered by passing a `scope` dictionary.
    Some assumptions in the background system can also be adjusted by passing a `background_configuration` dictionary.

    .. code-block:: python

        scope = {
                        'powertrain':['BEV', 'FCEV', 'ICEV-p'],
                    }
        bc = {'country':'CH', # considers electricity network losses for Switzerland
              'custom electricity mix' : [[1,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
                                          [0,1,0,0,0,0,0,0,0,0,0,0,0,0,0],
                                          [0,0,1,0,0,0,0,0,0,0,0,0,0,0,0],
                                          [0,0,0,1,0,0,0,0,0,0,0,0,0,0,0],
                                         ], # in this case, 100% nuclear for the second year
              'fuel blend':{
                  'cng':{ #specify fuel bland for compressed gas
                        'primary fuel':{
                            'type':'biogas',
                            'share':[0.9, 0.8, 0.7, 0.6] # shares per year. Must total 1 for each year.
                            },
                        'secondary fuel':{
                            'type':'syngas',
                            'share': [0.1, 0.2, 0.3, 0.4]
                            }
                        },
                 'diesel':{
                        'primary fuel':{
                            'type':'synthetic diesel',
                            'share':[0.9, 0.8, 0.7, 0.6]
                            },
                        'secondary fuel':{
                            'type':'biodiesel - cooking oil',
                            'share': [0.1, 0.2, 0.3, 0.4]
                            }
                        },
                 'petrol':{
                        'primary fuel':{
                            'type':'petrol',
                            'share':[0.9, 0.8, 0.7, 0.6]
                            },
                        'secondary fuel':{
                            'type':'bioethanol - wheat straw',
                            'share': [0.1, 0.2, 0.3, 0.4]
                            }
                        },
                'hydrogen':{
                        'primary fuel':{'type':'electrolysis', 'share':[1, 0, 0, 0]},
                        'secondary fuel':{'type':'smr - natural gas', 'share':[0, 1, 1, 1]}
                        }
                    },
              'energy storage': {
                  'electric': {
                      'type':'NMC',
                      'origin': 'NO'
                  },
                  'hydrogen': {
                      'type':'carbon fiber'
                  }
              }
             }

        InventoryCalculation(CarModel.array,
                            background_configuration=background_configuration,
                            scope=scope,
                            scenario="RCP26")

    The `custom electricity mix` key in the background_configuration dictionary defines an electricity mix to apply,
    under the form of one or several array(s), depending on teh number of years to analyze,
    that should total 1, of which the indices correspond to:

        - [0]: hydro-power
        - [1]: nuclear
        - [2]: natural gas
        - [3]: solar power
        - [4]: wind power
        - [5]: biomass
        - [6]: coal
        - [7]: oil
        - [8]: geothermal
        - [9]: waste incineration
        - [10]: biogas with CCS
        - [11]: biomass with CCS
        - [12]: coal with CCS
        - [13]: natural gas with CCS
        - [14]: wood with CCS

    If none is given, the electricity mix corresponding to the country specified in `country` will be selected.
    If no country is specified, Europe applies.

    The `primary` and `secondary` fuel keys contain an array with shares of alternative petrol fuel for each year, to create a custom blend.
    If none is provided, a blend provided by the Integrated Assessment model REMIND is used, which will depend on the REMIND energy scenario selected.

    Here is a list of available fuel pathways:


    Hydrogen technologies
    --------------------
    "electrolysis"
    "smr - natural gas"
    "smr - natural gas with CCS"
    "smr - biogas"
    "smr - biogas with CCS"
    "coal gasification"
    "wood gasification"
    "wood gasification with CCS"
    "wood gasification with EF"
    "wood gasification with EF with CCS"
    "atr - natural gas"
    "atr - natural gas with CCS"
    "atr - biogas"
    "atr - biogas with CCS"


    Natural gas technologies
    ------------------------
    cng
    biogas - sewage sludge
    biogas - biowaste
    syngas

    Diesel technologies
    -------------------
    diesel
    biodiesel - algae
    biodiesel - cooking oil
    synthetic diesel

    Petrol technologies
    -------------------
    petrol
    bioethanol - wheat straw
    bioethanol - maize starch
    bioethanol - sugarbeet
    bioethanol - forest residues
    synthetic gasoline

    :ivar array: array from the CarModel class
    :vartype array: CarModel.array
    :ivar scope: dictionary that contains filters for narrowing the analysis
    :ivar background_configuration: dictionary that contains choices for background system
    :ivar scenario: REMIND energy scenario to use ("SSP2-Baseline": business-as-usual,
                                                    "SSP2-PkBudg1100": limits cumulative GHG emissions to 1,100 gigatons by 2100,
                                                    "static": no forward-looking modification of the background inventories).
                    "SSP2-Baseline" selected by default.

    .. code-block:: python

    """

    def __init__(
        self,
        array,
        scope=None,
        background_configuration=None,
        scenario="SSP2-Base",
        method="recipe",
        method_type="midpoint",
    ):

        if scope is None:
            scope = {}
            scope["size"] = array.coords["size"].values.tolist()
            scope["powertrain"] = array.coords["powertrain"].values.tolist()
            scope["year"] = array.coords["year"].values.tolist()
            scope["fu"] = {"unit": "vkm", "quantity": 1}
        else:
            scope["size"] = scope.get("size", array.coords["size"].values.tolist())
            scope["powertrain"] = scope.get(
                "powertrain", array.coords["powertrain"].values.tolist()
            )
            scope["year"] = scope.get("year", array.coords["year"].values.tolist())
            scope["fu"] = scope.get("fu", {"unit": "vkm", "quantity": 1})

            if "unit" not in scope["fu"]:
                scope["fu"]["unit"] = "vkm"
            else:
                if scope["fu"]["unit"] not in ["vkm", "pkm"]:
                    raise NameError(
                        "Incorrect specification of functional unit. Must be 'vkm' or 'pkm'."
                    )

            if "quantity" not in scope["fu"]:
                scope["fu"]["quantity"] = 1
            else:
                try:
                    float(scope["fu"]["quantity"])
                except ValueError:
                    raise ValueError(
                        "Incorrect quantity for the functional unit defined."
                    )

        self.scope = scope
        self.scenario = scenario

        # Check if a fleet composition is specified
        if "fleet" in self.scope["fu"]:
            # check if a path is provided
            if isinstance(self.scope["fu"]["fleet"], str):
                fp = Path(self.scope["fu"]["fleet"])

                if not fp.is_file():
                    raise FileNotFoundError(
                        "The CSV file that contains fleet composition could not be found."
                    )

                if fp.suffix != ".csv":
                    raise TypeError(
                        "A CSV file is expected to build the fleet composition."
                    )

                self.fleet = self.build_fleet_array(fp)

            else:
                if isinstance(self.scope["fu"]["fleet"], xr.DataArray):
                    self.fleet = self.scope["fu"]["fleet"]
                else:
                    raise TypeError(
                        "The format used to specify fleet compositions is not valid."
                        "A file path that points to a CSV file is expected. "
                        "Or an array of type xarray.DataArray."
                    )
        else:
            self.fleet = None

        array = array.sel(
            powertrain=self.scope["powertrain"],
            year=self.scope["year"],
            size=self.scope["size"],
        )

        self.array = array.stack(desired=["size", "powertrain", "year"])

        self.iterations = len(array.value.values)

        self.number_of_cars = (
            len(self.scope["size"])
            * len(self.scope["powertrain"])
            * len(self.scope["year"])
        )

        self.array_inputs = {
            x: i for i, x in enumerate(list(self.array.parameter.values), 0)
        }
        self.array_powertrains = {
            x: i for i, x in enumerate(list(self.array.powertrain.values), 0)
        }

        if not background_configuration is None:
            self.background_configuration = background_configuration
        else:
            self.background_configuration = {}

        if "energy storage" not in self.background_configuration:
            self.background_configuration["energy storage"] = {
                "electric": {"type": "NMC", "origin": "CN"}
            }
        else:
            if "electric" not in self.background_configuration["energy storage"]:
                self.background_configuration["energy storage"]["electric"] = {
                    "type": "NMC",
                    "origin": "CN",
                }
            else:
                if (
                    "origin"
                    not in self.background_configuration["energy storage"]["electric"]
                ):
                    self.background_configuration["energy storage"]["electric"][
                        "origin"
                    ] = "CN"
                if (
                    "type"
                    not in self.background_configuration["energy storage"]["electric"]
                ):
                    self.background_configuration["energy storage"]["electric"][
                        "type"
                    ] = "NMC"

        self.inputs = self.get_dict_input()
        self.bs = BackgroundSystemModel()
        self.country = self.get_country_of_use()
        self.add_additional_activities()
        self.rev_inputs = self.get_rev_dict_input()
        self.A = self.get_A_matrix()
        self.mix = self.define_electricity_mix_for_fuel_prep()
        self.fuel_blends = {}
        self.define_fuel_blends()
        self.set_actual_range()

        self.index_cng = [self.inputs[i] for i in self.inputs if "ICEV-g" in i[0]]
        self.index_combustion_wo_cng = [
            self.inputs[i]
            for i in self.inputs
            if any(
                ele in i[0]
                for ele in ["ICEV-p", "HEV-p", "PHEV-p", "ICEV-d", "PHEV-d", "HEV-d"]
            )
        ]
        self.index_diesel = [self.inputs[i] for i in self.inputs if "ICEV-d" in i[0]]
        self.index_all_diesel = [
            self.inputs[i]
            for i in self.inputs
            if any(ele in i[0] for ele in ["ICEV-d", "HEV-d", "PHEV-d"])
        ]
        self.index_all_petrol = [
            self.inputs[i]
            for i in self.inputs
            if any(ele in i[0] for ele in ["ICEV-p", "HEV-p", "PHEV-p"])
        ]
        self.index_petrol = [self.inputs[i] for i in self.inputs if "ICEV-p" in i[0]]
        self.index_hybrid = [
            self.inputs[i]
            for i in self.inputs
            if any(ele in i[0] for ele in ["HEV-p", "HEV-d"])
        ]
        self.index_plugin_hybrid = [
            self.inputs[i] for i in self.inputs if "PHEV" in i[0]
        ]
        self.index_fuel_cell = [self.inputs[i] for i in self.inputs if "FCEV" in i[0]]

        self.map_non_fuel_emissions = {
            (
                "Methane, fossil",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Methane direct emissions, suburban",
            (
                "Methane, fossil",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Methane direct emissions, rural",
            (
                "Lead",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Lead direct emissions, suburban",
            (
                "Ammonia",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Ammonia direct emissions, suburban",
            (
                "NMVOC, non-methane volatile organic compounds, unspecified origin",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "NMVOC direct emissions, urban",
            (
                "Dinitrogen monoxide",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Dinitrogen oxide direct emissions, rural",
            (
                "Nitrogen oxides",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Nitrogen oxides direct emissions, urban",
            (
                "Ammonia",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Ammonia direct emissions, urban",
            (
                "Particulates, < 2.5 um",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Particulate matters direct emissions, suburban",
            (
                "Carbon monoxide, fossil",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Carbon monoxide direct emissions, urban",
            (
                "Nitrogen oxides",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Nitrogen oxides direct emissions, rural",
            (
                "NMVOC, non-methane volatile organic compounds, unspecified origin",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "NMVOC direct emissions, suburban",
            (
                "Benzene",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Benzene direct emissions, suburban",
            (
                "Ammonia",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Ammonia direct emissions, rural",
            (
                "NMVOC, non-methane volatile organic compounds, unspecified origin",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "NMVOC direct emissions, rural",
            (
                "Particulates, < 2.5 um",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Particulate matters direct emissions, urban",
            (
                "Dinitrogen monoxide",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Dinitrogen oxide direct emissions, suburban",
            (
                "Carbon monoxide, fossil",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Carbon monoxide direct emissions, rural",
            (
                "Methane, fossil",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Methane direct emissions, urban",
            (
                "Carbon monoxide, fossil",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Carbon monoxide direct emissions, suburban",
            (
                "Lead",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Lead direct emissions, urban",
            (
                "Particulates, < 2.5 um",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Particulate matters direct emissions, rural",
            (
                "Benzene",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Benzene direct emissions, rural",
            (
                "Nitrogen oxides",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Nitrogen oxides direct emissions, suburban",
            (
                "Lead",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Lead direct emissions, rural",
            (
                "Benzene",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Benzene direct emissions, urban",
            (
                "Dinitrogen monoxide",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Dinitrogen oxide direct emissions, urban",
            (
                "Toluene",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Toluene direct emissions, urban",
            (
                "Toluene",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Toluene direct emissions, suburban",
            (
                "Toluene",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Toluene direct emissions, rural",
            (
                "Xylene",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Xylene direct emissions, urban",
            (
                "Xylene",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Xylene direct emissions, suburban",
            (
                "Xylene",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Xylene direct emissions, rural",
            (
                "Formaldehyde",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Formaldehyde direct emissions, urban",
            (
                "Formaldehyde",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Formaldehyde direct emissions, suburban",
            (
                "Formaldehyde",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Formaldehyde direct emissions, rural",
            (
                "Acetaldehyde",
                ("air", "urban air close to ground"),
                "kilogram",
            ): "Acetaldehyde direct emissions, urban",
            (
                "Acetaldehyde",
                ("air", "non-urban air or from high stacks"),
                "kilogram",
            ): "Acetaldehyde direct emissions, suburban",
            (
                "Acetaldehyde",
                ("air", "low population density, long-term"),
                "kilogram",
            ): "Acetaldehyde direct emissions, rural",
        }

        self.index_emissions = [
            self.inputs[i] for i in self.map_non_fuel_emissions.keys()
        ]

        self.map_noise_emissions = {
            (
                "noise, octave 1, day time, urban",
                ("octave 1", "day time", "urban"),
                "joule",
            ): "noise, octave 1, day time, urban",
            (
                "noise, octave 2, day time, urban",
                ("octave 2", "day time", "urban"),
                "joule",
            ): "noise, octave 2, day time, urban",
            (
                "noise, octave 3, day time, urban",
                ("octave 3", "day time", "urban"),
                "joule",
            ): "noise, octave 3, day time, urban",
            (
                "noise, octave 4, day time, urban",
                ("octave 4", "day time", "urban"),
                "joule",
            ): "noise, octave 4, day time, urban",
            (
                "noise, octave 5, day time, urban",
                ("octave 5", "day time", "urban"),
                "joule",
            ): "noise, octave 5, day time, urban",
            (
                "noise, octave 6, day time, urban",
                ("octave 6", "day time", "urban"),
                "joule",
            ): "noise, octave 6, day time, urban",
            (
                "noise, octave 7, day time, urban",
                ("octave 7", "day time", "urban"),
                "joule",
            ): "noise, octave 7, day time, urban",
            (
                "noise, octave 8, day time, urban",
                ("octave 8", "day time", "urban"),
                "joule",
            ): "noise, octave 8, day time, urban",
            (
                "noise, octave 1, day time, suburban",
                ("octave 1", "day time", "suburban"),
                "joule",
            ): "noise, octave 1, day time, suburban",
            (
                "noise, octave 2, day time, suburban",
                ("octave 2", "day time", "suburban"),
                "joule",
            ): "noise, octave 2, day time, suburban",
            (
                "noise, octave 3, day time, suburban",
                ("octave 3", "day time", "suburban"),
                "joule",
            ): "noise, octave 3, day time, suburban",
            (
                "noise, octave 4, day time, suburban",
                ("octave 4", "day time", "suburban"),
                "joule",
            ): "noise, octave 4, day time, suburban",
            (
                "noise, octave 5, day time, suburban",
                ("octave 5", "day time", "suburban"),
                "joule",
            ): "noise, octave 5, day time, suburban",
            (
                "noise, octave 6, day time, suburban",
                ("octave 6", "day time", "suburban"),
                "joule",
            ): "noise, octave 6, day time, suburban",
            (
                "noise, octave 7, day time, suburban",
                ("octave 7", "day time", "suburban"),
                "joule",
            ): "noise, octave 7, day time, suburban",
            (
                "noise, octave 8, day time, suburban",
                ("octave 8", "day time", "suburban"),
                "joule",
            ): "noise, octave 8, day time, suburban",
            (
                "noise, octave 1, day time, rural",
                ("octave 1", "day time", "rural"),
                "joule",
            ): "noise, octave 1, day time, rural",
            (
                "noise, octave 2, day time, rural",
                ("octave 2", "day time", "rural"),
                "joule",
            ): "noise, octave 2, day time, rural",
            (
                "noise, octave 3, day time, rural",
                ("octave 3", "day time", "rural"),
                "joule",
            ): "noise, octave 3, day time, rural",
            (
                "noise, octave 4, day time, rural",
                ("octave 4", "day time", "rural"),
                "joule",
            ): "noise, octave 4, day time, rural",
            (
                "noise, octave 5, day time, rural",
                ("octave 5", "day time", "rural"),
                "joule",
            ): "noise, octave 5, day time, rural",
            (
                "noise, octave 6, day time, rural",
                ("octave 6", "day time", "rural"),
                "joule",
            ): "noise, octave 6, day time, rural",
            (
                "noise, octave 7, day time, rural",
                ("octave 7", "day time", "rural"),
                "joule",
            ): "noise, octave 7, day time, rural",
            (
                "noise, octave 8, day time, rural",
                ("octave 8", "day time", "rural"),
                "joule",
            ): "noise, octave 8, day time, rural",
        }

        self.elec_map = {
            "Hydro": (
                "electricity production, hydro, run-of-river",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Nuclear": (
                "electricity production, nuclear, pressure water reactor",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Gas": (
                "electricity production, natural gas, conventional power plant",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Solar": (
                "electricity production, photovoltaic, 3kWp slanted-roof installation, multi-Si, panel, mounted",
                "DE",
                "kilowatt hour",
                "electricity, low voltage",
            ),
            "Wind": (
                "electricity production, wind, 1-3MW turbine, onshore",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Biomass": (
                "heat and power co-generation, wood chips, 6667 kW, state-of-the-art 2014",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Coal": (
                "electricity production, hard coal",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Oil": (
                "electricity production, oil",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Geo": (
                "electricity production, deep geothermal",
                "DE",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Waste": (
                "treatment of municipal solid waste, incineration",
                "DE",
                "kilowatt hour",
                "electricity, for reuse in municipal waste incineration only",
            ),
            "Biomass CCS": (
                "electricity production, at BIGCC power plant 450MW, pre, pipeline 200km, storage 1000m",
                "RER",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Coal CCS": (
                "electricity production, at power plant/hard coal, post, pipeline 200km, storage 1000m",
                "RER",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Gas CCS": (
                "electricity production, at power plant/natural gas, post, pipeline 200km, storage 1000m",
                "RER",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Biogas CCS": (
                "electricity production, at power plant/biogas, post, pipeline 200km, storage 1000m",
                "RER",
                "kilowatt hour",
                "electricity, high voltage",
            ),
            "Wood CCS": (
                "electricity production, at wood burning power plant 20 MW, truck 25km, post, pipeline 200km, storage 1000m",
                "RER",
                "kilowatt hour",
                "electricity, high voltage",
            ),
        }

        self.index_noise = [self.inputs[i] for i in self.map_noise_emissions.keys()]

        self.list_cat, self.split_indices = self.get_split_indices()

        self.method = method

        if self.method == "recipe":
            self.method_type = method_type
        else:
            self.method_type = "midpoint"

        self.impact_categories = self.get_dict_impact_categories()

        # Load the B matrix
        self.B = self.get_B_matrix()

    def __getitem__(self, key):
        """
        Make class['foo'] automatically filter for the parameter 'foo'
        Makes the model code much cleaner

        :param key: Parameter name
        :type key: str
        :return: `array` filtered after the parameter selected
        """
        return self.temp_array.sel(parameter=key)

    def build_fleet_array(self, fp):
        """
        Receives a file path that points to a CSV file that contains the fleet composition
        Checks that the fleet composition array is valid.

        Specifically:

        * the years specified in the fleet must be present in scope["year"]
        * the powertrains specified in the fleet must be present in scope["powertrain"]
        * the sizes specified in the fleet must be present in scope["size"]
        * the sum for each year-powertrain-size set must equal 1

        :param fp: filepath to an array that contains fleet composition
        :type fp: str
        :return array: fleet composition array
        :rtype array: xarray.DataArray
        """
        arr = pd.read_csv(fp, delimiter=";", header=0, index_col=[0, 1, 2])
        arr = arr.fillna(0)
        arr.columns = [int(c) for c in arr.columns]

        new_cols = [c for c in self.scope["year"] if c not in arr.columns]
        arr[new_cols] = pd.DataFrame([[0] * len(new_cols)], index=arr.index)

        a = [self.scope["powertrain"]] + [self.scope["size"]] + [self.scope["year"]]

        for row in [i for i in list(itertools.product(*a)) if i not in arr.index]:
            arr.loc[row] = 0

        array = arr.to_xarray()
        array = array.rename(
            {"level_0": "powertrain", "level_1": "size", "level_2": "vintage_year"}
        )

        if not set(list(array.data_vars)).issubset(self.scope["year"]):
            raise ValueError(
                "The fleet years differ from {}".format(self.scope["year"])
            )

        if set(self.scope["year"]) != set(array.coords["vintage_year"].values.tolist()):
            raise ValueError(
                "The list of vintage years differ from {}.".format(self.scope["year"])
            )

        if not set(array.coords["powertrain"].values.tolist()).issubset(
            self.scope["powertrain"]
        ):
            raise ValueError(
                "The fleet powertrain list differs from {}".format(
                    self.scope["powertrain"]
                )
            )

        if not set(array.coords["size"].values.tolist()).issubset(self.scope["size"]):
            raise ValueError(
                "The fleet size list differs from {}".format(self.scope["size"])
            )

        # try:
        #    assert np.allclose(array.sum(dim=["vintage_year", "powertrain", "size"]).to_array(), np.ones_like(array.sum().to_array()))
        # except AssertionError:
        #    raise AssertionError("The sums of vehicle shares year-wise are not equal to 1.")

        return array.to_array().fillna(0)

    def get_results_table(self, split, sensitivity=False):
        """
        Format an xarray.DataArray array to receive the results.

        :param split: "components" or "impact categories". Split by impact categories only applicable when "endpoint" level is applied.
        :return: xarrray.DataArray
        """

        if split == "components":
            cat = [
                "direct - exhaust",
                "direct - non-exhaust",
                "energy chain",
                "maintenance",
                "glider",
                "EoL",
                "powertrain",
                "energy storage",
                "road",
            ]

        dict_impact_cat = list(self.impact_categories.keys())

        sizes = self.scope["size"]

        if isinstance(self.fleet, xr.core.dataarray.DataArray):
            sizes += ["fleet average"]

        if sensitivity == False:

            response = xr.DataArray(
                np.zeros(
                    (
                        self.B.shape[1],
                        len(sizes),
                        len(self.scope["powertrain"]),
                        len(self.scope["year"]),
                        len(cat),
                        self.iterations,
                    )
                ),
                coords=[
                    dict_impact_cat,
                    sizes,
                    self.scope["powertrain"],
                    self.scope["year"],
                    cat,
                    np.arange(0, self.iterations),
                ],
                dims=[
                    "impact_category",
                    "size",
                    "powertrain",
                    "year",
                    "impact",
                    "value",
                ],
            )

        else:
            params = [a for a in self.array.value.values]
            response = xr.DataArray(
                np.zeros(
                    (
                        self.B.shape[1],
                        len(sizes),
                        len(self.scope["powertrain"]),
                        len(self.scope["year"]),
                        self.iterations,
                    )
                ),
                coords=[
                    dict_impact_cat,
                    sizes,
                    self.scope["powertrain"],
                    self.scope["year"],
                    params,
                ],
                dims=["impact_category", "size", "powertrain", "year", "parameter"],
            )

        return response

    def get_split_indices(self):
        """
        Return list of indices to split the results into categories.

        :return: list of indices
        :rtype: list
        """
        filename = "dict_split.csv"
        filepath = DATA_DIR / filename
        if not filepath.is_file():
            raise FileNotFoundError("The dictionary of splits could not be found.")

        with open(filepath) as f:
            csv_list = [[val.strip() for val in r.split(";")] for r in f.readlines()]
        (_, _, *header), *data = csv_list

        csv_dict = {}
        for row in data:
            key, sub_key, *values = row

            if key in csv_dict:
                if sub_key in csv_dict[key]:
                    csv_dict[key][sub_key].append(
                        {"search by": values[0], "search for": values[1]}
                    )
                else:
                    csv_dict[key][sub_key] = [
                        {"search by": values[0], "search for": values[1]}
                    ]
            else:
                csv_dict[key] = {
                    sub_key: [{"search by": values[0], "search for": values[1]}]
                }

        flatten = itertools.chain.from_iterable

        d = {}
        l = []

        d["direct - exhaust"] = []
        d["direct - exhaust"].append(
            self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[
                ("Carbon dioxide, from soil or biomass stock", ("air",), "kilogram")
            ]
        )
        d["direct - exhaust"].append(
            self.inputs[("Methane, fossil", ("air",), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Cadmium", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Copper", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Chromium", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Nickel", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Selenium", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[("Zinc", ("air", "urban air close to ground"), "kilogram")]
        )
        d["direct - exhaust"].append(
            self.inputs[
                ("Chromium VI", ("air", "urban air close to ground"), "kilogram")
            ]
        )
        d["direct - exhaust"].extend(self.index_emissions)
        d["direct - exhaust"].extend(self.index_noise)

        l.append(d["direct - exhaust"])

        for cat in csv_dict["components"]:
            d[cat] = list(
                flatten(
                    [
                        self.get_index_of_flows([l["search for"]], l["search by"])
                        for l in csv_dict["components"][cat]
                    ]
                )
            )
            l.append(d[cat])

        list_ind = [d[x] for x in d]
        maxLen = max(map(len, list_ind))
        for row in list_ind:
            while len(row) < maxLen:
                row.extend([len(self.inputs) - 1])
        return list(d.keys()), list_ind

    def calculate_impacts(self, split="components", sensitivity=False):

        # Prepare an array to store the results
        results = self.get_results_table(split, sensitivity=sensitivity)

        # Create electricity and fuel market datasets
        self.create_electricity_market_for_fuel_prep()

        # Create electricity market dataset for battery production
        self.create_electricity_market_for_battery_production()

        # Fill in the A matrix with car parameters
        self.set_inputs_in_A_matrix(self.array.values)

        # Add rows for fleet vehicles, if any
        if isinstance(self.fleet, xr.core.dataarray.DataArray):
            self.build_fleet_vehicles()

            # Update number of cars
            self.number_of_cars += len(self.scope["year"]) * len(
                self.scope["powertrain"]
            )

            # Update dictionary
            self.get_rev_dict_input()

            # Update B matrix
            self.B = self.get_B_matrix()

        new_arr = np.float32(
            np.zeros((self.A.shape[1], self.B.shape[1], len(self.scope["year"])))
        )

        f = np.float32(np.zeros((np.shape(self.A)[1])))

        # Collect indices of activities contributing to the first level
        ind_cars = [self.inputs[i] for i in self.inputs if "transport, passenger" in i[0]]
        arr = self.A[0, : -self.number_of_cars, ind_cars].sum(axis=0)
        ind = np.nonzero(arr)[0]

        if self.scenario != "static":
            B = self.B.interp(
                year=self.scope["year"], kwargs={"fill_value": "extrapolate"}
            ).values
        else:
            B = self.B[0].values

        for a in ind:
            f[:] = 0
            f[a] = 1
            X = np.float32(sparse.linalg.spsolve(self.A[0], f.T))

            if self.scenario == "static":
                new_arr[a] = np.float32(X * B).sum(axis=-1).T[..., None]
            else:
                new_arr[a] = np.float32(X * B).sum(axis=-1).T

        shape = (
            self.iterations,
            len(self.scope["size"]),
            len(self.scope["powertrain"]),
            len(self.scope["year"]),
            self.A.shape[1],
        )

        arr = (
            self.A[:, :, -self.number_of_cars:].transpose(0, 2, 1).reshape(shape)
            * new_arr.transpose(1, 2, 0)[:, None, None, None, ...]
            * -1
        )
        arr = arr[..., self.split_indices].sum(axis=-1)

        results[...] = arr.transpose(0, 2, 3, 4, 5, 1)

        if sensitivity:
            results /= results.sel(parameter="reference")

        # If the FU is in passenger-km, we normalize the results by the number of passengers
        if self.scope["fu"]["unit"] == "vkm":
            load_factor = 1
        else:

            load_factor = np.resize(
                self.array[self.array_inputs["average passengers"]].values,
                (
                    1,
                    len(self.scope["size"]),
                    len(self.scope["powertrain"]),
                    len(self.scope["year"]),
                    1,
                    1,
                ),
            )

        return (
            results.astype("float32") / load_factor * int(self.scope["fu"]["quantity"])
        )

    def add_additional_activities(self):
        # Add as many rows and columns as cars to consider
        # Also add additional columns and rows for electricity markets
        # for fuel preparation and energy battery production

        maximum = max(self.inputs.values())

        for y in self.scope["year"]:

            if {"ICEV-p", "HEV-p", "PHEV-p"}.intersection(
                set(self.scope["powertrain"])
            ):
                maximum += 1
                self.inputs[
                    (
                        "fuel supply for gasoline vehicles, " + str(y),
                        self.country,
                        "kilogram",
                        "fuel",
                    )
                ] = maximum

            if {"ICEV-d", "HEV-d", "PHEV-d"}.intersection(
                set(self.scope["powertrain"])
            ):
                maximum += 1
                self.inputs[
                    (
                        "fuel supply for diesel vehicles, " + str(y),
                        self.country,
                        "kilogram",
                        "fuel",
                    )
                ] = maximum

            if {"ICEV-g"}.intersection(set(self.scope["powertrain"])):
                maximum += 1
                self.inputs[
                    (
                        "fuel supply for gas vehicles, " + str(y),
                        self.country,
                        "kilogram",
                        "fuel",
                    )
                ] = maximum

            if {"FCEV"}.intersection(set(self.scope["powertrain"])):
                maximum += 1
                self.inputs[
                    (
                        "fuel supply for hydrogen vehicles, " + str(y),
                        self.country,
                        "kilogram",
                        "fuel",
                    )
                ] = maximum

            if {"BEV", "PHEV-p", "PHEV-d"}.intersection(set(self.scope["powertrain"])):
                maximum += 1
                self.inputs[
                    (
                        "electricity supply for electric vehicles, " + str(y),
                        self.country,
                        "kilowatt hour",
                        "electricity, low voltage, for battery electric vehicles",
                    )
                ] = maximum

            maximum += 1
            self.inputs[
                (
                    "electricity market for fuel preparation, " + str(y),
                    self.country,
                    "kilowatt hour",
                    "electricity, low voltage",
                )
            ] = maximum

            maximum += 1
            self.inputs[
                (
                    "electricity market for energy storage production, " + str(y),
                    self.background_configuration["energy storage"]["electric"][
                        "origin"
                    ],
                    "kilowatt hour",
                    "electricity, low voltage, for energy storage production",
                )
            ] = maximum

        for s in self.scope["size"]:
            for pt in self.scope["powertrain"]:
                for y in self.scope["year"]:
                    maximum += 1

                    if y < 1993:
                        euro_class = "EURO-0"
                    if 1993 <= y < 1997:
                        euro_class = "EURO-1"
                    if 1997 <= y < 2001:
                        euro_class = "EURO-2"
                    if 2001 <= y < 2006:
                        euro_class = "EURO-3"
                    if 2006 <= y < 2011:
                        euro_class = "EURO-4"
                    if 2011 <= y < 2015:
                        euro_class = "EURO-5"
                    if 2015 <= y < 2017:
                        euro_class = "EURO-6ab"
                    if 2017 <= y < 2019:
                        euro_class = "EURO-6c"
                    if 2019 <= y < 2020:
                        euro_class = "EURO-6d-TEMP"
                    if y >= 2020:
                        euro_class = "EURO-6d"

                    name = (
                        "transport, passenger car, "
                        + pt
                        + ", "
                        + s
                        + ", "
                        + str(y)
                        + ", "
                        + euro_class
                    )

                    if self.scope["fu"]["unit"] == "vkm":
                        unit = "kilometer"
                    if self.scope["fu"]["unit"] == "pkm":
                        unit = "person kilometer"

                    self.inputs[
                        (
                            name,
                            self.background_configuration["country"],
                            unit,
                            "transport, passenger car, " + euro_class,
                        )
                    ] = maximum

    def add_additional_activities_for_export(self):
        # Add as many rows and columns as cars to consider
        # Also add additional columns and rows for electricity markets
        # for fuel preparation and energy battery production

        maximum = max(self.inputs.values())

        for s in self.scope["size"]:
            for pt in self.scope["powertrain"]:
                for y in self.scope["year"]:
                    maximum += 1

                    if y < 1993:
                        euro_class = "EURO-0"
                    if 1993 <= y < 1997:
                        euro_class = "EURO-1"
                    if 1997 <= y < 2001:
                        euro_class = "EURO-2"
                    if 2001 <= y < 2006:
                        euro_class = "EURO-3"
                    if 2006 <= y < 2011:
                        euro_class = "EURO-4"
                    if 2011 <= y < 2015:
                        euro_class = "EURO-5"
                    if 2015 <= y < 2017:
                        euro_class = "EURO-6ab"
                    if 2017 <= y < 2019:
                        euro_class = "EURO-6c"
                    if 2019 <= y < 2020:
                        euro_class = "EURO-6d-TEMP"
                    if y >= 2020:
                        euro_class = "EURO-6d"

                    name = (
                        "Passenger car, "
                        + pt
                        + ", "
                        + s
                        + ", "
                        + str(y)
                        + ", "
                        + euro_class
                    )

                    self.inputs[
                        (
                            name,
                            self.background_configuration["country"],
                            "unit",
                            "Passenger car, " + euro_class,
                        )
                    ] = maximum

    def get_A_matrix(self):
        """
        Load the A matrix. The A matrix contains exchanges of products (rows) between activities (columns).

        :return: A matrix with three dimensions of shape (number of values, number of products, number of activities).
        :rtype: numpy.ndarray

        """
        filename = "A_matrix.csv"
        filepath = (
            Path(getframeinfo(currentframe()).filename)
            .resolve()
            .parent.joinpath("data/" + filename)
        )
        if not filepath.is_file():
            raise FileNotFoundError("The technology matrix could not be found.")

        initial_A = np.genfromtxt(filepath, delimiter=";")

        new_A = np.identity(len(self.inputs))

        new_A[0 : np.shape(initial_A)[0], 0 : np.shape(initial_A)[0]] = initial_A

        # Resize the matrix to fit the number of iterations in `array`
        new_A = np.resize(new_A, (self.array.shape[1], new_A.shape[0], new_A.shape[1]))
        return new_A.astype("float32")

    def build_fleet_vehicles(self):

        # additional cars
        n_cars = len(self.scope["year"]) * len(self.scope["powertrain"])
        self.A = np.pad(self.A, ((0, 0), (0, n_cars), (0, n_cars)))

        maximum = max(self.inputs.values())

        for pt in self.scope["powertrain"]:

            for y in self.scope["year"]:

                # share of the powertrain that year, all sizes
                share_pt = self.fleet.sel(powertrain=pt, variable=y).sum().values

                name = "transport, passenger car, fleet average, " + pt + ", " + str(y)

                maximum += 1

                if self.scope["fu"]["unit"] == "vkm":
                    unit = "kilometer"
                if self.scope["fu"]["unit"] == "pkm":
                    unit = "person kilometer"

                self.inputs[
                    (
                        name,
                        self.background_configuration["country"],
                        unit,
                        "transport, passenger car, fleet average",
                    )
                ] = maximum

                self.A[:, maximum, maximum] = 1

                if share_pt > 0:
                    for s in self.fleet.coords["size"].values:
                        for vin_year in self.scope["year"]:
                            fleet_share = (
                                self.fleet.sel(
                                    powertrain=pt,
                                    vintage_year=vin_year,
                                    size=s,
                                    variable=y,
                                )
                                .sum()
                                .values
                                / share_pt
                            )

                            if fleet_share > 0:

                                car_index = [
                                    self.inputs[i]
                                    for i in self.inputs
                                    if all(
                                        [
                                            item in i[0]
                                            for item in [pt, str(vin_year), s]
                                        ]
                                    )
                                ][0]
                                car_inputs = (
                                    self.A[:, : car_index - 1, car_index] * fleet_share
                                )

                                self.A[:, : car_index - 1, maximum] += car_inputs

                    # Fuel and electricity supply must be from the fleet year, not the vintage year

                    d_map_fuel = {
                        "ICEV-p": "gasoline",
                        "ICEV-d": "diesel",
                        "ICEV-g": "gas",
                        "HEV-p": "gasoline",
                        "HEV-d": "diesel",
                        "PHEV-p": "gasoline",
                        "PHEV-d": "diesel",
                        "BEV": "electric",
                        "FCEV": "hydrogen",
                    }

                    ind_supply = [
                        self.inputs[i]
                        for i in self.inputs
                        if "supply for " + d_map_fuel[pt] + " vehicles, " in i[0]
                    ]
                    amount_fuel = self.A[:, ind_supply, maximum].sum(axis=1)

                    # zero out initial fuel inputs
                    self.A[:, ind_supply, maximum] = 0

                    # set saved amount to current fuel supply provider
                    current_provider = [
                        self.inputs[i]
                        for i in self.inputs
                        if "supply for " + d_map_fuel[pt] + " vehicles, " + str(y)
                        in i[0]
                    ]
                    self.A[:, current_provider, maximum] = amount_fuel

                    if pt in ["PHEV-p", "PHEV-d"]:
                        ind_supply = [
                            self.inputs[i]
                            for i in self.inputs
                            if "supply for electric vehicles, " in i[0]
                        ]
                        amount_fuel = self.A[:, ind_supply, maximum].sum(axis=1)

                        # zero out initial fuel inputs
                        self.A[:, ind_supply, maximum] = 0

                        # set saved amount to current fuel supply provider
                        current_provider = [
                            self.inputs[i]
                            for i in self.inputs
                            if "supply for electric vehicles, " + str(y) in i[0]
                        ]
                        self.A[:, current_provider, maximum] = amount_fuel

    def get_B_matrix(self):
        """
        Load the B matrix. The B matrix contains impact assessment figures for a give impact assessment method,
        per unit of activity. Its length column-wise equals the length of the A matrix row-wise.
        Its length row-wise equals the number of impact assessment methods.

        :param method: only "recipe" and "ilcd" available at the moment.
        :param level: only "midpoint" available at the moment.
        :return: an array with impact values per unit of activity for each method.
        :rtype: numpy.ndarray

        """

        if self.method == "recipe":
            if self.method_type == "midpoint":
                list_file_names = glob.glob(
                    str(REMIND_FILES_DIR)
                    + "/*recipe_midpoint*{}*.csv".format(self.scenario)
                )
                B = np.zeros((len(list_file_names), 21, len(self.inputs)))
            else:
                list_file_names = glob.glob(
                    str(REMIND_FILES_DIR)
                    + "/*recipe_endpoint*{}*.csv".format(self.scenario)
                )
                B = np.zeros((len(list_file_names), 3, len(self.inputs)))

        else:
            list_file_names = glob.glob(
                str(REMIND_FILES_DIR) + "/*ilcd*{}*.csv".format(self.scenario)
            )
            B = np.zeros((len(list_file_names), 19, len(self.inputs)))

        for f, fp in enumerate(list_file_names):
            initial_B = np.genfromtxt(fp, delimiter=";")

            new_B = np.zeros((np.shape(initial_B)[0], len(self.inputs),))

            new_B[0 : np.shape(initial_B)[0], 0 : np.shape(initial_B)[1]] = initial_B

            B[f, :, :] = new_B

        list_impact_categories = list(self.impact_categories.keys())

        if self.scenario != "static":
            response = xr.DataArray(
                B,
                coords=[
                    [2005, 2010, 2020, 2030, 2040, 2050],
                    list_impact_categories,
                    list(self.inputs.keys()),
                ],
                dims=["year", "category", "activity"],
            )
        else:
            response = xr.DataArray(
                B,
                coords=[[2020], list_impact_categories, list(self.inputs.keys()),],
                dims=["year", "category", "activity"],
            )

        return response

    def get_dict_input(self):
        """
        Load a dictionary with tuple ("name of activity", "location", "unit", "reference product") as key, row/column
        indices as values.

        :return: dictionary with `label:index` pairs.
        :rtype: dict

        """
        filename = "dict_inputs_A_matrix.csv"
        filepath = DATA_DIR / filename
        if not filepath.is_file():
            raise FileNotFoundError(
                "The dictionary of activity labels could not be found."
            )
        csv_dict = {}
        count = 0
        with open(filepath) as f:
            input_dict = csv.reader(f, delimiter=";")
            for row in input_dict:
                if "(" in row[1]:
                    new_str = row[1].replace("(", "")
                    new_str = new_str.replace(")", "")
                    new_str = [s.strip() for s in new_str.split(",") if s]
                    t = ()
                    for s in new_str:
                        if "low population" in s:
                            s = "low population density, long-term"
                            t += (s,)
                            break
                        else:
                            t += (s.replace("'", ""),)
                    csv_dict[(row[0], t, row[2])] = count
                else:
                    csv_dict[(row[0], row[1], row[2], row[3])] = count
                count += 1

        return csv_dict

    def get_dict_impact_categories(self):
        """
        Load a dictionary with available impact assessment methods as keys, and assessment level and categories as values.

        ..code-block:: python

            {'recipe': {'midpoint': ['freshwater ecotoxicity',
                                   'human toxicity',
                                   'marine ecotoxicity',
                                   'terrestrial ecotoxicity',
                                   'metal depletion',
                                   'agricultural land occupation',
                                   'climate change',
                                   'fossil depletion',
                                   'freshwater eutrophication',
                                   'ionising radiation',
                                   'marine eutrophication',
                                   'natural land transformation',
                                   'ozone depletion',
                                   'particulate matter formation',
                                   'photochemical oxidant formation',
                                   'terrestrial acidification',
                                   'urban land occupation',
                                   'water depletion',
                                   'human noise',
                                   'primary energy, non-renewable',
                                   'primary energy, renewable']
                       }
           }

        :return: dictionary
        :rtype: dict
        """
        filename = "dict_impact_categories.csv"
        filepath = DATA_DIR / filename
        if not filepath.is_file():
            raise FileNotFoundError(
                "The dictionary of impact categories could not be found."
            )

        csv_dict = {}

        with open(filepath) as f:
            input_dict = csv.reader(f, delimiter=";")
            for row in input_dict:
                if row[0] == self.method and row[3] == self.method_type:
                    csv_dict[row[2]] = {
                        "method": row[1],
                        "category": row[2],
                        "type": row[3],
                        "abbreviation": row[4],
                        "unit": row[5],
                        "source": row[6],
                    }

        return csv_dict

    def get_rev_dict_input(self):
        """
        Reverse the self.inputs dictionary.

        :return: reversed dictionary
        :rtype: dict
        """
        return {v: k for k, v in self.inputs.items()}

    def get_index_vehicle_from_array(
        self, items_to_look_for, items_to_look_for_also=None, method="or"
    ):
        """
        Return list of row/column indices of self.array of labels that contain the string defined in `items_to_look_for`.

        :param items_to_look_for: string to search for
        :return: list
        """
        if not isinstance(items_to_look_for, list):
            items_to_look_for = [items_to_look_for]

        if not items_to_look_for_also is None:
            if not isinstance(items_to_look_for_also, list):
                items_to_look_for_also = [items_to_look_for_also]

        list_vehicles = self.array.desired.values

        if method == "or":
            return [
                c
                for c, v in enumerate(list_vehicles)
                if set(items_to_look_for).intersection(v)
            ]

        if method == "and":
            return [
                c
                for c, v in enumerate(list_vehicles)
                if set(items_to_look_for).intersection(v)
                and set(items_to_look_for_also).intersection(v)
            ]

    def get_index_of_flows(self, items_to_look_for, search_by="name"):
        """
        Return list of row/column indices of self.A of labels that contain the string defined in `items_to_look_for`.

        :param items_to_look_for: string
        :param search_by: "name" or "compartment" (for elementary flows)
        :return: list of row/column indices
        :rtype: list
        """
        if search_by == "name":
            return [
                int(self.inputs[c])
                for c in self.inputs
                if all(ele in c[0].lower() for ele in items_to_look_for)
            ]
        if search_by == "compartment":
            return [
                int(self.inputs[c])
                for c in self.inputs
                if all(ele in c[1] for ele in items_to_look_for)
            ]

    def export_lci(
        self,
        presamples=True,
        ecoinvent_compatibility=True,
        ecoinvent_version="3.7",
        db_name="carculator db",
        forbidden_activities=None,
    ):
        """
        Export the inventory as a dictionary. Also return a list of arrays that contain pre-sampled random values if
        :meth:`stochastic` of :class:`CarModel` class has been called.

        :param presamples: boolean.
        :param ecoinvent_compatibility: bool. If True, compatible with ecoinvent. If False, compatible with REMIND-ecoinvent.
        :param ecoinvent_version: str. "3.5", "3.6" or "uvek"
        :return: inventory, and optionally, list of arrays containing pre-sampled values.
        :rtype: list
        """

        self.inputs = self.get_dict_input()
        self.bs = BackgroundSystemModel()
        self.country = self.get_country_of_use()
        self.add_additional_activities()
        self.rev_inputs = self.get_rev_dict_input()
        self.A = self.get_A_matrix()

        # add vehicles datasets
        self.add_additional_activities_for_export()

        # Update dictionary
        self.rev_inputs = self.get_rev_dict_input()

        # resize A matrix
        self.A = self.get_A_matrix()

        # Create electricity and fuel market datasets
        self.create_electricity_market_for_fuel_prep()

        # Create electricity market dataset for battery production
        self.create_electricity_market_for_battery_production()

        # Create fuel markets
        self.fuel_blends = {}
        self.define_fuel_blends()
        self.set_actual_range()

        self.set_inputs_in_A_matrix_for_export(self.array.values)

        # Add rows for fleet vehicles, if any
        if isinstance(self.fleet, xr.core.dataarray.DataArray):
            print("Building fleet average vehicles...")
            self.build_fleet_vehicles()

            # Update dictionary
            self.rev_inputs = self.get_rev_dict_input()

            # Update number of cars
            self.number_of_cars += len(self.scope["year"]) * len(
                self.scope["powertrain"]
            )

        if presamples == True:
            lci, array = ExportInventory(
                self.A, self.rev_inputs, db_name=db_name
            ).write_lci(
                presamples,
                ecoinvent_compatibility,
                ecoinvent_version,
                forbidden_activities,
            )
            return (lci, array)
        else:
            lci = ExportInventory(self.A, self.rev_inputs, db_name=db_name).write_lci(
                presamples,
                ecoinvent_compatibility,
                ecoinvent_version,
                forbidden_activities,
            )
            return lci

    def export_lci_to_bw(
        self,
        presamples=True,
        ecoinvent_compatibility=True,
        ecoinvent_version="3.7",
        db_name="carculator db",
        forbidden_activities=None,
    ):
        """
        Export the inventory as a `brightway2` bw2io.importers.base_lci.LCIImporter object
        with the inventory in the `data` attribute.

        .. code-block:: python

            # get the inventory
            i, _ = ic.export_lci_to_bw()

            # import it in a Brightway2 project
            i.match_database('ecoinvent 3.6 cutoff', fields=('name', 'unit', 'location', 'reference product'))
            i.match_database("biosphere3", fields=('name', 'unit', 'categories'))
            i.match_database(fields=('name', 'unit', 'location', 'reference product'))
            i.match_database(fields=('name', 'unit', 'categories'))

            # Create an additional biosphere database for the few flows that do not
            # exist in "biosphere3"
            i.create_new_biosphere("additional_biosphere", relink=True)

            # Check if all exchanges link
            i.statistics()

            # Register the database
            i.write_database()

        :return: LCIImport object that can be directly registered in a `brightway2` project.
        :rtype: bw2io.importers.base_lci.LCIImporter
        """

        self.inputs = self.get_dict_input()
        self.bs = BackgroundSystemModel()
        self.country = self.get_country_of_use()
        self.add_additional_activities()
        self.rev_inputs = self.get_rev_dict_input()
        self.A = self.get_A_matrix()

        # add vehicles datasets
        self.add_additional_activities_for_export()

        # Update dictionary
        self.rev_inputs = self.get_rev_dict_input()

        # resize A matrix
        self.A = self.get_A_matrix()

        # Create electricity and fuel market datasets
        self.create_electricity_market_for_fuel_prep()

        # Create electricity market dataset for battery production
        self.create_electricity_market_for_battery_production()

        # Create fuel markets
        self.fuel_blends = {}
        self.define_fuel_blends()
        self.set_actual_range()

        self.set_inputs_in_A_matrix_for_export(self.array.values)

        # Add rows for fleet vehicles, if any
        if isinstance(self.fleet, xr.core.dataarray.DataArray):
            print("Building fleet average vehicles...")
            self.build_fleet_vehicles()

            # Update dictionary
            self.rev_inputs = self.get_rev_dict_input()

            # Update number of cars
            self.number_of_cars += len(self.scope["year"]) * len(
                self.scope["powertrain"]
            )

        if presamples == True:
            lci, array = ExportInventory(
                self.A, self.rev_inputs, db_name=db_name
            ).write_lci_to_bw(
                presamples,
                ecoinvent_compatibility,
                ecoinvent_version,
                forbidden_activities,
            )
            return (lci, array)
        else:
            lci = ExportInventory(
                self.A, self.rev_inputs, db_name=db_name
            ).write_lci_to_bw(
                presamples,
                ecoinvent_compatibility,
                ecoinvent_version,
                forbidden_activities,
            )
            return lci

    def export_lci_to_excel(
        self,
        directory=None,
        ecoinvent_compatibility=True,
        ecoinvent_version="3.7",
        software_compatibility="brightway2",
        filename=None,
        forbidden_activities=None,
    ):
        """
        Export the inventory as an Excel file (if the destination software is Brightway2) or a CSV file (if the destination software is Simapro) file.
        Also return the file path where the file is stored.

        :param directory: directory where to save the file.
        :type directory: str
        :param ecoinvent_compatibility: If True, compatible with ecoinvent. If False, compatible with REMIND-ecoinvent.
        :param ecoinvent_version: "3.6", "3.5" or "uvek"
        :param software_compatibility: "brightway2" or "simapro"
        :return: file path where the file is stored.
        :rtype: str
        """

        if software_compatibility not in ("brightway2", "simapro"):
            raise NameError(
                "The destination software argument is not valid. Choose between 'brightway2' or 'simapro'."
            )

        # Simapro inventory only for ecoinvent 3.5 or UVEK
        if software_compatibility == "simapro":
            if ecoinvent_version == "3.6":
                print(
                    "Simapro-compatible inventory export is only available for ecoinvent 3.5 or UVEK."
                )
                return
            ecoinvent_compatibility = True
            ecoinvent_version = "3.5"

        self.inputs = self.get_dict_input()
        self.bs = BackgroundSystemModel()
        self.country = self.get_country_of_use()
        self.add_additional_activities()
        self.rev_inputs = self.get_rev_dict_input()
        self.A = self.get_A_matrix()

        # add vehicles datasets
        self.add_additional_activities_for_export()

        # Update dictionary
        self.rev_inputs = self.get_rev_dict_input()

        # resize A matrix
        self.A = self.get_A_matrix()

        # Create electricity and fuel market datasets
        self.create_electricity_market_for_fuel_prep()

        # Create fuel markets
        self.fuel_blends = {}
        self.define_fuel_blends()
        self.set_actual_range()

        # Create electricity market dataset for battery production
        self.create_electricity_market_for_battery_production()

        self.set_inputs_in_A_matrix_for_export(self.array.values)

        # Add rows for fleet vehicles, if any
        if isinstance(self.fleet, xr.core.dataarray.DataArray):
            print("Building fleet average vehicles...")
            self.build_fleet_vehicles()

            # Update dictionary
            self.rev_inputs = self.get_rev_dict_input()

            # Update number of cars
            self.number_of_cars += len(self.scope["year"]) * len(
                self.scope["powertrain"]
            )

        fp = ExportInventory(
            self.A, self.rev_inputs, db_name=filename or "carculator db"
        ).write_lci_to_excel(
            directory,
            ecoinvent_compatibility,
            ecoinvent_version,
            software_compatibility,
            filename,
            forbidden_activities,
        )
        return fp

    def get_country_of_use(self):

        if "country" not in self.background_configuration:
            self.background_configuration["country"] = "RER"

        return self.background_configuration["country"]

    def define_electricity_mix_for_fuel_prep(self):
        """
        This function defines a fuel mix based either on user-defined mix, or on default mixes for a given country.
        The mix is calculated as the average mix, weighted by the distribution of annually driven kilometers.
        :return:
        """
        try:
            losses_to_low = float(self.bs.losses[self.country]["LV"])
        except KeyError:
            # If losses for the country are not found, assume EU average
            losses_to_low = float(self.bs.losses["RER"]["LV"])

        if "custom electricity mix" in self.background_configuration:
            # If a special electricity mix is specified, we use it
            mix = self.background_configuration["custom electricity mix"]

        else:
            use_year = (
                (
                    self.array.values[self.array_inputs["lifetime kilometers"]]
                    / self.array.values[self.array_inputs["kilometers per year"]]
                )
                .reshape(
                    self.iterations,
                    len(self.scope["powertrain"]),
                    len(self.scope["size"]),
                    len(self.scope["year"]),
                )
                .mean(axis=(0, 1, 2))
            )

            if self.country not in self.bs.electricity_mix.country.values:
                print(
                    "The electricity mix for {} could not be found. Average European electricity mix is used instead.".format(
                        self.country
                    )
                )
                country = "RER"
            else:
                country = self.country

            mix = [
                self.bs.electricity_mix.sel(
                    country=country,
                    variable=[
                        "Hydro",
                        "Nuclear",
                        "Gas",
                        "Solar",
                        "Wind",
                        "Biomass",
                        "Coal",
                        "Oil",
                        "Geothermal",
                        "Waste",
                        "Biogas CCS",
                        "Biomass CCS",
                        "Coal CCS",
                        "Gas CCS",
                        "Wood CCS",
                    ],
                )
                .interp(
                    year=np.arange(year, year + use_year[y]),
                    kwargs={"fill_value": "extrapolate"},
                )
                .mean(axis=0)
                .values
                if y + use_year[y] <= 2050
                else self.bs.electricity_mix.sel(
                    country=country,
                    variable=[
                        "Hydro",
                        "Nuclear",
                        "Gas",
                        "Solar",
                        "Wind",
                        "Biomass",
                        "Coal",
                        "Oil",
                        "Geothermal",
                        "Waste",
                        "Biogas CCS",
                        "Biomass CCS",
                        "Coal CCS",
                        "Gas CCS",
                        "Wood CCS",
                    ],
                )
                .interp(
                    year=np.arange(year, 2051), kwargs={"fill_value": "extrapolate"}
                )
                .mean(axis=0)
                .values
                for y, year in enumerate(self.scope["year"])
            ]
        return mix

    def define_renewable_rate_in_mix(self):

        try:
            losses_to_low = float(self.bs.losses[self.country]["LV"])
        except KeyError:
            # If losses for the country are not found, assume EU average
            losses_to_low = float(self.bs.losses["RER"]["LV"])

        category_name = (
            "climate change"
            if self.method == "recipe"
            else "climate change - climate change total"
        )


        if self.method_type != "endpoint":
            if self.scenario != "static":
                year = self.scope["year"]
                co2_intensity_tech = (
                         self.B.sel(category=category_name, activity=list(self.elec_map.values()), )
                         .interp(year=year, kwargs={"fill_value": "extrapolate"})
                         .values
                         * losses_to_low
                 ) * 1000
            else:
                year = 2020
                co2_intensity_tech = np.resize((
                self.B.sel(category=category_name, activity=list(self.elec_map.values()), year=year)
                .values
                * losses_to_low
                * 1000), (len(self.scope["year"]), 15))
        else:
            co2_intensity_tech = np.zeroes((len(self.scope["year"]), 15))

        sum_renew = [
            np.sum([self.mix[x][i] for i in [0, 3, 4, 5, 8]])
            for x in range(0, len(self.mix))
        ]

        return sum_renew, co2_intensity_tech

    def create_electricity_market_for_fuel_prep(self):
        """ This function fills the electricity market that supplies battery charging operations
        and hydrogen production through electrolysis.
        """

        try:
            losses_to_low = float(self.bs.losses[self.country]["LV"])
        except KeyError:
            # If losses for the country are not found, assume EU average
            losses_to_low = float(self.bs.losses["RER"]["LV"])

        # Fill the electricity markets for battery charging and hydrogen production
        for y, year in enumerate(self.scope["year"]):
            m = np.array(self.mix[y]).reshape(-1, 15, 1)
            # Add electricity technology shares
            self.A[
                np.ix_(
                    np.arange(self.iterations),
                    [self.inputs[self.elec_map[t]] for t in self.elec_map],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "electricity market for fuel preparation" in i[0]
                    ],
                )
            ] = (m * -1 * losses_to_low)

            # Add transmission network for high and medium voltage
            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, electricity, high voltage",
                        "CH",
                        "kilometer",
                        "transmission network, electricity, high voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = (6.58e-9 * -1 * losses_to_low)

            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, electricity, medium voltage",
                        "CH",
                        "kilometer",
                        "transmission network, electricity, medium voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = (1.86e-8 * -1 * losses_to_low)

            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, long-distance",
                        "UCTE",
                        "kilometer",
                        "transmission network, long-distance",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = (3.17e-10 * -1 * losses_to_low)

            # Add distribution network, low voltage
            self.A[
                :,
                self.inputs[
                    (
                        "distribution network construction, electricity, low voltage",
                        "CH",
                        "kilometer",
                        "distribution network, electricity, low voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = (8.74e-8 * -1 * losses_to_low)

            # Add supply of sulfur hexafluoride for transformers
            self.A[
                :,
                self.inputs[
                    (
                        "market for sulfur hexafluoride, liquid",
                        "RER",
                        "kilogram",
                        "sulfur hexafluoride, liquid",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = ((5.4e-8 + 2.99e-9) * -1 * losses_to_low)

            # Add SF_6 leakage

            self.A[
                :,
                self.inputs[("Sulfur hexafluoride", ("air",), "kilogram")],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for fuel preparation" in i[0]
                ],
            ] = ((5.4e-8 + 2.99e-9) * -1 * losses_to_low)

    def create_electricity_market_for_battery_production(self):
        """
        This function fills in the column in `self.A` concerned with the electricity mix used for manufacturing battery cells
        :return:
        """

        battery_tech = self.background_configuration["energy storage"]["electric"][
            "type"
        ]
        battery_origin = self.background_configuration["energy storage"]["electric"][
            "origin"
        ]

        try:
            losses_to_low = float(self.bs.losses[battery_origin]["LV"])
        except KeyError:
            losses_to_low = float(self.bs.losses["CN"]["LV"])

        mix_battery_manufacturing = (
            self.bs.electricity_mix.sel(
                country=battery_origin,
                variable=[
                    "Hydro",
                    "Nuclear",
                    "Gas",
                    "Solar",
                    "Wind",
                    "Biomass",
                    "Coal",
                    "Oil",
                    "Geothermal",
                    "Waste",
                    "Biogas CCS",
                    "Biomass CCS",
                    "Coal CCS",
                    "Gas CCS",
                    "Wood CCS",
                ],
            )
            .interp(year=self.scope["year"], kwargs={"fill_value": "extrapolate"})
            .values
        )

        # Fill the electricity markets for battery production
        for y, year in enumerate(self.scope["year"]):
            m = np.array(mix_battery_manufacturing[y]).reshape(-1, 15, 1)

            self.A[
                np.ix_(
                    np.arange(self.iterations),
                    [self.inputs[self.elec_map[t]] for t in self.elec_map],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "electricity market for energy storage production" in i[0]
                    ],
                )
            ] = (m * losses_to_low * -1)

            # Add transmission network for high and medium voltage
            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, electricity, high voltage",
                        "CH",
                        "kilometer",
                        "transmission network, electricity, high voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = (6.58e-9 * -1 * losses_to_low)

            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, electricity, medium voltage",
                        "CH",
                        "kilometer",
                        "transmission network, electricity, medium voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = (1.86e-8 * -1 * losses_to_low)

            self.A[
                :,
                self.inputs[
                    (
                        "transmission network construction, long-distance",
                        "UCTE",
                        "kilometer",
                        "transmission network, long-distance",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = (3.17e-10 * -1 * losses_to_low)

            # Add distribution network, low voltage
            self.A[
                :,
                self.inputs[
                    (
                        "distribution network construction, electricity, low voltage",
                        "CH",
                        "kilometer",
                        "distribution network, electricity, low voltage",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = (8.74e-8 * -1 * losses_to_low)

            # Add supply of sulfur hexafluoride for transformers
            self.A[
                :,
                self.inputs[
                    (
                        "market for sulfur hexafluoride, liquid",
                        "RER",
                        "kilogram",
                        "sulfur hexafluoride, liquid",
                    )
                ],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = ((5.4e-8 + 2.99e-9) * -1 * losses_to_low)

            # Add SF_6 leakage

            self.A[
                :,
                self.inputs[("Sulfur hexafluoride", ("air",), "kilogram")],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "electricity market for energy storage production" in i[0]
                ],
            ] = ((5.4e-8 + 2.99e-9) * -1 * losses_to_low)

    def get_share_biofuel(self):
        try:
            region = self.bs.region_map[self.country]["RegionCode"]
        except KeyError:
            print(
                "Could not find biofuel share information for the region/country specified. "
                "Will use European biofuel share instead."
            )
            region = "EUR"
        scenario = self.scenario if self.scenario != "static" else "SSP2-Base"

        share_biofuel = (
            self.bs.biofuel.sel(
                region=region, value=0, fuel_type="Biomass fuel", scenario=scenario,
            )
            .interp(year=self.scope["year"], kwargs={"fill_value": "extrapolate"})
            .values
        )
        return share_biofuel

    def find_fuel_shares(self, fuel_type):

        default_fuels = {
            "petrol": {
                "primary": "petrol",
                "secondary": "bioethanol - wheat straw",
                "third": "bioethanol - maize starch",
            },
            "diesel": {
                "primary": "diesel",
                "secondary": "biodiesel - cooking oil",
                "third": "biodiesel - algae",
            },
            "cng": {
                "primary": "cng",
                "secondary": "biogas - sewage sludge",
                "third": "biogas - biowaste",
            },
            "hydrogen": {
                "primary": "electrolysis",
                "secondary": "smr - natural gas",
                "third": "atr - natural gas",
            },
        }

        tertiary = None
        tertiary_share = None

        if "fuel blend" in self.background_configuration:
            if fuel_type in self.background_configuration["fuel blend"]:
                primary = self.background_configuration["fuel blend"][fuel_type][
                    "primary fuel"
                ]["type"]

                if "secondary fuel" in self.background_configuration["fuel blend"][fuel_type]:
                    secondary = self.background_configuration["fuel blend"][fuel_type][
                        "secondary fuel"
                    ]["type"]
                else:

                    if default_fuels[fuel_type]["secondary"] != primary:
                        secondary = default_fuels[fuel_type]["secondary"]
                        self.background_configuration["fuel blend"][fuel_type][
                            "secondary fuel"] = {"type": secondary}
                    else:
                        secondary = default_fuels[fuel_type]["third"]
                        self.background_configuration["fuel blend"][fuel_type][
                            "secondary fuel"]= {"type": secondary}

                if "tertiary fuel" in self.background_configuration["fuel blend"][fuel_type]:
                    tertiary = self.background_configuration["fuel blend"][fuel_type]["tertiary fuel"]["type"]

                primary_share = self.background_configuration["fuel blend"][fuel_type][
                    "primary fuel"
                ]["share"]

                if "share" not in self.background_configuration["fuel blend"][fuel_type][
                            "secondary fuel"
                        ]:
                    secondary_share = 1 - np.asarray(primary_share)
                else:
                    secondary_share = self.background_configuration["fuel blend"][fuel_type][
                        "secondary fuel"
                    ]["share"]

                if tertiary:
                    tertiary_share = self.background_configuration["fuel blend"][fuel_type][
                    "tertiary fuel"
                ]["share"]


            else:
                primary = default_fuels[fuel_type]["primary"]
                secondary = default_fuels[fuel_type]["secondary"]
                secondary_share = self.get_share_biofuel()
                primary_share = 1 - np.array(secondary_share)
        else:
            primary = default_fuels[fuel_type]["primary"]
            secondary = default_fuels[fuel_type]["secondary"]
            secondary_share = self.get_share_biofuel()
            primary_share = 1 - np.array(secondary_share)

        return (primary, secondary, primary_share, secondary_share, tertiary, tertiary_share)

    def set_actual_range(self):
        """
        Set the actual range considering the blend.
        Liquid bio-fuels and synthetic fuels typically have a lower calorific value. Hence, the need to recalculate
        the vehicle range.
        Modifies parameter `range` of `array` in place
        """

        if {"ICEV-p", "HEV-p", "PHEV-p"}.intersection(set(self.scope["powertrain"])):
            for y, year in enumerate(self.scope["year"]):

                share_primary = self.fuel_blends["petrol"]["primary"]["share"][y]
                lhv_primary = self.fuel_blends["petrol"]["primary"]["lhv"]
                share_secondary = self.fuel_blends["petrol"]["secondary"]["share"][y]
                lhv_secondary = self.fuel_blends["petrol"]["secondary"]["lhv"]
                index = self.get_index_vehicle_from_array(
                    ["ICEV-p", "HEV-p", "PHEV-p"], year, method="and"
                )

                self.array.values[self.array_inputs["range"], :, index] = (
                    (
                        (
                            self.array.values[self.array_inputs["fuel mass"], :, index]
                            * share_primary
                            * lhv_primary
                        )
                        + (
                            self.array.values[self.array_inputs["fuel mass"], :, index]
                            * share_secondary
                            * lhv_secondary
                        )
                    )
                    * 1000
                    / self.array.values[self.array_inputs["TtW energy"], :, index]
                )

        if {"ICEV-d", "HEV-d", "PHEV-d"}.intersection(set(self.scope["powertrain"])):
            for y, year in enumerate(self.scope["year"]):
                share_primary = self.fuel_blends["diesel"]["primary"]["share"][y]
                lhv_primary = self.fuel_blends["diesel"]["primary"]["lhv"]
                share_secondary = self.fuel_blends["diesel"]["secondary"]["share"][y]
                lhv_secondary = self.fuel_blends["diesel"]["secondary"]["lhv"]
                index = self.get_index_vehicle_from_array(
                    ["ICEV-d", "PHEV-d", "HEV-d"], year, method="and"
                )

                self.array.values[self.array_inputs["range"], :, index] = (
                    (
                        (
                            self.array.values[self.array_inputs["fuel mass"], :, index]
                            * share_primary
                            * lhv_primary
                        )
                        + (
                            self.array.values[self.array_inputs["fuel mass"], :, index]
                            * share_secondary
                            * lhv_secondary
                        )
                    )
                    * 1000
                    / self.array.values[self.array_inputs["TtW energy"], :, index]
                )

    def define_fuel_blends(self):
        """
        This function defines fuel blends from what is passed in `background_configuration`.
        It populates a dictionary `self.fuel_blends` that contains the respective shares, lower heating values
        and CO2 emission factors of the fuels used.
        :return:
        """

        fuels_lhv = {
            "petrol": 42.4,
            "bioethanol - wheat straw": 26.8,
            "bioethanol - maize starch": 26.8,
            "bioethanol - sugarbeet": 26.8,
            "bioethanol - forest residues": 26.8,
            "synthetic gasoline": 42.4,
            "diesel": 42.8,
            "biodiesel - cooking oil": 31.7,
            "biodiesel - algae": 31.7,
            "synthetic diesel": 43.3,
            "cng": 55.5,
            "biogas - sewage sludge": 55.5,
            "biogas - biowaste": 55.5,
            "syngas": 55.5,
        }

        fuels_CO2 = {
            "petrol": 3.18,
            "bioethanol - wheat straw": 1.91,
            "bioethanol - maize starch": 1.91,
            "bioethanol - sugarbeet": 1.91,
            "bioethanol - forest residues": 1.91,
            "synthetic gasoline": 3.18,
            "diesel": 3.14,
            "biodiesel - cooking oil": 2.85,
            "biodiesel - algae": 2.85,
            "synthetic diesel": 3.16,
            "cng": 2.65,
            "biogas - sewage sludge": 2.65,
            "biogas - biowaste": 2.65,
            "syngas": 2.65,
        }

        if {"ICEV-p", "HEV-p", "PHEV-p"}.intersection(set(self.scope["powertrain"])):
            fuel_type = "petrol"
            primary, secondary, primary_share, secondary_share, tertiary, tertiary_share = self.find_fuel_shares(
                fuel_type
            )
            self.create_fuel_markets(
                fuel_type, primary, secondary, tertiary, primary_share, secondary_share, tertiary_share
            )
            self.fuel_blends[fuel_type] = {
                "primary": {
                    "type": primary,
                    "share": primary_share,
                    "lhv": fuels_lhv[primary],
                    "CO2": fuels_CO2[primary],
                },
                "secondary": {
                    "type": secondary,
                    "share": secondary_share,
                    "lhv": fuels_lhv[secondary],
                    "CO2": fuels_CO2[secondary],
                }
            }

            if tertiary:
                self.fuel_blends[fuel_type]["tertiary"] = {
                    "type": tertiary,
                    "share": tertiary_share,
                    "lhv": fuels_lhv[tertiary],
                    "CO2": fuels_CO2[tertiary],
                }


        if {"ICEV-d", "HEV-d", "PHEV-d"}.intersection(set(self.scope["powertrain"])):
            fuel_type = "diesel"
            primary, secondary, primary_share, secondary_share, tertiary, tertiary_share = self.find_fuel_shares(
                fuel_type
            )
            self.create_fuel_markets(
                fuel_type, primary, secondary, tertiary, primary_share, secondary_share, tertiary_share
            )
            self.fuel_blends[fuel_type] = {
                "primary": {
                    "type": primary,
                    "share": primary_share,
                    "lhv": fuels_lhv[primary],
                    "CO2": fuels_CO2[primary],
                },
                "secondary": {
                    "type": secondary,
                    "share": secondary_share,
                    "lhv": fuels_lhv[secondary],
                    "CO2": fuels_CO2[secondary],
                },
            }

            if tertiary:
                self.fuel_blends[fuel_type]["tertiary"] = {
                    "type": tertiary,
                    "share": tertiary_share,
                    "lhv": fuels_lhv[tertiary],
                    "CO2": fuels_CO2[tertiary],
                }

        if {"ICEV-g"}.intersection(set(self.scope["powertrain"])):
            fuel_type = "cng"
            primary, secondary, primary_share, secondary_share, tertiary, tertiary_share = self.find_fuel_shares(
                fuel_type
            )
            self.create_fuel_markets(
                fuel_type, primary, secondary, tertiary, primary_share, secondary_share, tertiary_share
            )
            self.fuel_blends[fuel_type] = {
                "primary": {
                    "type": primary,
                    "share": primary_share,
                    "lhv": fuels_lhv[primary],
                    "CO2": fuels_CO2[primary],
                },
                "secondary": {
                    "type": secondary,
                    "share": secondary_share,
                    "lhv": fuels_lhv[primary],
                    "CO2": fuels_CO2[primary],
                },
            }

            if tertiary:
                self.fuel_blends[fuel_type]["tertiary"] = {
                    "type": tertiary,
                    "share": tertiary_share,
                    "lhv": fuels_lhv[tertiary],
                    "CO2": fuels_CO2[tertiary],
                }

        if {"FCEV"}.intersection(set(self.scope["powertrain"])):
            fuel_type = "hydrogen"
            primary, secondary, primary_share, secondary_share, tertiary, tertiary_share = self.find_fuel_shares(
                fuel_type
            )
            self.create_fuel_markets(
                fuel_type, primary, secondary, tertiary, primary_share, secondary_share, tertiary_share
            )
            self.fuel_blends[fuel_type] = {
                "primary": {"type": primary, "share": primary_share},
                "secondary": {"type": secondary, "share": secondary_share},
            }

            if tertiary:
                self.fuel_blends[fuel_type]["tertiary"] = {
                    "type": tertiary,
                    "share": tertiary_share
                }

        if {"BEV", "PHEV-p", "PHEV-d"}.intersection(set(self.scope["powertrain"])):
            fuel_type = "electricity"
            self.create_fuel_markets(fuel_type)

    def create_fuel_markets(
        self,
        fuel_type,
        primary=None,
        secondary=None,
        tertiary=None,
        primary_share=None,
        secondary_share=None,
        tertiary_share=None
    ):
        """
        This function creates markets for fuel, considering a given blend, a given fuel type and a given year.
        It also adds separate electricity input in case hydrogen from electrolysis is needed somewhere in the fuel supply chain.
        :return:
        """

        d_fuels = {
            "electrolysis": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from electrolysis, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 58,
            },
            "smr - natural gas": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from SMR of NG, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "smr - natural gas with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from SMR of NG, with CCS, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "smr - biogas": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from SMR of biogas, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "smr - biogas with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from SMR of biogas with CCS, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "coal gasification": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from coal gasification, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from dual fluidised bed gasification of woody biomass, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from dual fluidised bed gasification of woody biomass with CCS, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with EF": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from gasification of woody biomass in oxy-fired entrained flow gasifier, at fuelling plant",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with EF with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from gasification of woody biomass in oxy-fired entrained flow gasifier, with CCS, at fuelling plant",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification (Swiss forest)": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from dual fluidised bed gasification of woody biomass, at H2 fuelling station",
                    "CH",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with CCS (Swiss forest)": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from dual fluidised bed gasification of woody biomass with CCS, at H2 fuelling station",
                    "CH",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with EF (Swiss forest)": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from gasification of woody biomass in oxy-fired entrained flow gasifier, at fuelling station",
                    "CH",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "wood gasification with EF with CCS (Swiss forest)": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from gasification of woody biomass in oxy-fired entrained flow gasifier, with CCS, at fuelling station",
                    "CH",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "atr - natural gas": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, ATR of NG, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "atr - natural gas with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, ATR of NG, with CCS, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "atr - biogas": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from ATR of biogas, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "atr - biogas with CCS": {
                "name": (
                    "Hydrogen, gaseous, 700 bar, from ATR of biogas with CCS, at H2 fuelling station",
                    "RER",
                    "kilogram",
                    "Hydrogen, gaseous, 700 bar",
                ),
                "additional electricity": 0,
            },
            "cng": {
                "name": (
                    "market for natural gas, high pressure, vehicle grade",
                    "GLO",
                    "kilogram",
                    "natural gas, high pressure, vehicle grade",
                ),
                "additional electricity": 0,
            },
            "biogas - sewage sludge": {
                "name": (
                    "biogas upgrading - sewage sludge - amine scrubbing - best",
                    "CH",
                    "kilogram",
                    "biogas upgrading - sewage sludge - amine scrubbing - best",
                ),
                "additional electricity": 0,
            },
            "biogas - biowaste": {
                "name": (
                    "biomethane from biogas upgrading - biowaste - amine scrubbing",
                    "CH",
                    "kilogram",
                    "biomethane",
                ),
                "additional electricity": 0,
            },
            "syngas": {
                "name": (
                    "Methane production, synthetic, from electrochemical methanation",
                    "RER",
                    "kilogram",
                    "Methane, synthetic",
                ),
                "additional electricity": 58 * 0.50779661,
            },
            "diesel": {
                "name": (
                    "market for diesel",
                    "Europe without Switzerland",
                    "kilogram",
                    "diesel",
                ),
                "additional electricity": 0,
            },
            "biodiesel - algae": {
                "name": (
                    "Biodiesel from algae",
                    "RER",
                    "kilogram",
                    "Biodiesel from algae",
                ),
                "additional electricity": 0,
            },
            "biodiesel - cooking oil": {
                "name": (
                    "Biodiesel from cooking oil",
                    "RER",
                    "kilogram",
                    "Biodiesel from cooking oil",
                ),
                "additional electricity": 0,
            },
            "synthetic diesel": {
                "name": (
                    "Diesel production, synthetic, Fischer Tropsch process",
                    "RER",
                    "kilogram",
                    "Diesel, synthetic",
                ),
                "additional electricity": 58 * 0.2875,
            },
            "petrol": {
                "name": (
                    "market for petrol, low-sulfur",
                    "Europe without Switzerland",
                    "kilogram",
                    "petrol, low-sulfur",
                ),
                "additional electricity": 0,
            },
            "bioethanol - wheat straw": {
                "name": (
                    "Ethanol from wheat straw pellets",
                    "RER",
                    "kilogram",
                    "Ethanol from wheat straw pellets",
                ),
                "additional electricity": 0,
            },
            "bioethanol - forest residues": {
                "name": (
                    "Ethanol from forest residues",
                    "RER",
                    "kilogram",
                    "Ethanol from forest residues",
                ),
                "additional electricity": 0,
            },
            "bioethanol - sugarbeet": {
                "name": (
                    "Ethanol from sugarbeet",
                    "RER",
                    "kilogram",
                    "Ethanol from sugarbeet",
                ),
                "additional electricity": 0,
            },
            "bioethanol - maize starch": {
                "name": (
                    "Ethanol from maize starch",
                    "RER",
                    "kilogram",
                    "Ethanol from maize starch",
                ),
                "additional electricity": 0,
            },
            "synthetic gasoline": {
                "name": (
                    "Gasoline production, synthetic, from methanol",
                    "RER",
                    "kilogram",
                    "Gasoline, synthetic",
                ),
                "additional electricity": 58 * 0.328,
            },
        }

        d_dataset_name = {
            "petrol": "fuel supply for gasoline vehicles, ",
            "diesel": "fuel supply for diesel vehicles, ",
            "cng": "fuel supply for gas vehicles, ",
            "hydrogen": "fuel supply for hydrogen vehicles, ",
            "electricity": "electricity supply for electric vehicles, ",
        }

        if fuel_type != "electricity":
            for y, year in enumerate(self.scope["year"]):
                dataset_name = d_dataset_name[fuel_type] + str(year)
                fuel_market_index = [
                    self.inputs[i] for i in self.inputs if i[0] == dataset_name
                ][0]
                primary_fuel_activity_index = self.inputs[d_fuels[primary]["name"]]
                secondary_fuel_activity_index = self.inputs[d_fuels[secondary]["name"]]

                self.A[:, primary_fuel_activity_index, fuel_market_index] = (
                    -1 * primary_share[y]
                )
                self.A[:, secondary_fuel_activity_index, fuel_market_index] = (
                    -1 * secondary_share[y]
                )

                additional_electricity = (
                        d_fuels[primary]["additional electricity"] * primary_share[y]
                        + d_fuels[secondary]["additional electricity"] * secondary_share[y]
                        )

                if tertiary:
                    tertiary_fuel_activity_index = self.inputs[d_fuels[tertiary]["name"]]
                    self.A[:, tertiary_fuel_activity_index, fuel_market_index] = (
                        -1 * tertiary_share[y]
                    )
                    additional_electricity += d_fuels[tertiary]["additional electricity"] * tertiary_share[y]



                if additional_electricity > 0:
                    electricity_mix_index = [
                        self.inputs[i]
                        for i in self.inputs
                        if i[0]
                        == "electricity market for fuel preparation, " + str(year)
                    ][0]
                    self.A[:, electricity_mix_index, fuel_market_index] = (
                        -1 * additional_electricity
                    )
        else:
            for year in self.scope["year"]:
                dataset_name = d_dataset_name[fuel_type] + str(year)
                electricity_market_index = [
                    self.inputs[i] for i in self.inputs if i[0] == dataset_name
                ][0]
                electricity_mix_index = [
                    self.inputs[i]
                    for i in self.inputs
                    if i[0] == "electricity market for fuel preparation, " + str(year)
                ][0]
                self.A[:, electricity_mix_index, electricity_market_index] = -1

    def set_inputs_in_A_matrix(self, array):
        """
        Fill-in the A matrix. Does not return anything. Modifies in place.
        Shape of the A matrix (values, products, activities).

        :param array: :attr:`array` from :class:`CarModel` class
        """

        # Glider
        self.A[
            :,
            self.inputs[
                (
                    "market for glider, passenger car",
                    "GLO",
                    "kilogram",
                    "glider, passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            (array[self.array_inputs["glider base mass"], :])
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                ("Glider lightweighting", "GLO", "kilogram", "Glider lightweighting")
            ],
            -self.number_of_cars :,
        ] = (
            (
                array[self.array_inputs["lightweighting"], :]
                * array[self.array_inputs["glider base mass"], :]
            )
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "maintenance, passenger car",
                    "RER",
                    "unit",
                    "passenger car maintenance",
                )
            ],
            -self.number_of_cars :,
        ] = (array[self.array_inputs["curb mass"], :] / 1240 / 150000 * -1)

        # Glider EoL
        self.A[
            :,
            self.inputs[
                (
                    "market for manual dismantling of used electric passenger car",
                    "GLO",
                    "unit",
                    "manual dismantling of used electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["curb mass"], :]
            * (1 - array[self.array_inputs["combustion power share"], :])
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for manual dismantling of used passenger car with internal combustion engine",
                    "GLO",
                    "unit",
                    "manual dismantling of used passenger car with internal combustion engine",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["curb mass"], :]
            * array[self.array_inputs["combustion power share"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        # Powertrain components
        self.A[
            :,
            self.inputs[
                (
                    "market for charger, electric passenger car",
                    "GLO",
                    "kilogram",
                    "charger, electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["charger mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for converter, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "converter, for electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["converter mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for electric motor, electric passenger car",
                    "GLO",
                    "kilogram",
                    "electric motor, electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["electric engine mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for inverter, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "inverter, for electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["inverter mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for power distribution unit, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "power distribution unit, for electric passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["power distribution unit mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        l_elec_pt = [
            "charger mass",
            "converter mass",
            "inverter mass",
            "power distribution unit mass",
            "electric engine mass",
            "fuel cell stack mass",
            "fuel cell ancillary BoP mass",
            "fuel cell essential BoP mass",
            "battery cell mass",
            "battery BoP mass",
        ]

        self.A[
            :,
            self.inputs[
                (
                    "market for used powertrain from electric passenger car, manual dismantling",
                    "GLO",
                    "kilogram",
                    "used powertrain from electric passenger car, manual dismantling",
                )
            ],
            -self.number_of_cars :,
        ] = (
            array[[self.array_inputs[l] for l in l_elec_pt], :].sum(axis=0)
            / array[self.array_inputs["lifetime kilometers"], :]
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for internal combustion engine, passenger car",
                    "GLO",
                    "kilogram",
                    "internal combustion engine, for passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (
            (
                array[
                    [
                        self.array_inputs[l]
                        for l in ["combustion engine mass", "powertrain mass"]
                    ],
                    :,
                ].sum(axis=0)
            )
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[("Ancillary BoP", "GLO", "kilogram", "Ancillary BoP")],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["fuel cell ancillary BoP mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[("Essential BoP", "GLO", "kilogram", "Essential BoP")],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["fuel cell essential BoP mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[("Stack", "GLO", "kilowatt", "Stack")],
            -self.number_of_cars :,
        ] = (
            array[self.array_inputs["fuel cell stack mass"], :]
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        # Start of printout

        print(
            "****************** IMPORTANT BACKGROUND PARAMETERS ******************",
            end="\n * ",
        )

        # Energy storage

        print(
            "The country of use is " + self.country, end="\n * ",
        )

        battery_tech = self.background_configuration["energy storage"]["electric"][
            "type"
        ]
        battery_origin = self.background_configuration["energy storage"]["electric"][
            "origin"
        ]

        print(
            "Power and energy batteries produced in "
            + battery_origin
            + " using "
            + battery_tech
            + " chemistry.",
            end="\n * ",
        )

        # Use the NMC inventory of Schmidt et al. 2019
        self.A[
            :,
            self.inputs[("Battery BoP", "GLO", "kilogram", "Battery BoP")],
            -self.number_of_cars :,
        ] = (
            (
                array[self.array_inputs["battery BoP mass"], :]
                * (1 + array[self.array_inputs["battery lifetime replacements"], :])
            )
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        battery_cell_label = (
            "Battery cell, " + battery_tech,
            "GLO",
            "kilogram",
            "Battery cell",
        )

        self.A[:, self.inputs[battery_cell_label], -self.number_of_cars :,] = (
            (
                array[self.array_inputs["battery cell mass"], :]
                * (1 + array[self.array_inputs["fuel cell lifetime replacements"], :])
            )
            / array[self.array_inputs["lifetime kilometers"], :]
            * -1
        )

        # Set an input of electricity, given the country of manufacture
        self.A[
            :,
            self.inputs[
                (
                    "market group for electricity, medium voltage",
                    "World",
                    "kilowatt hour",
                    "electricity, medium voltage",
                )
            ],
            self.inputs[battery_cell_label],
        ] = 0

        for y in self.scope["year"]:
            index = self.get_index_vehicle_from_array(y)

            self.A[
                np.ix_(
                    np.arange(self.iterations),
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0]
                        and "electricity market for energy storage production" in i[0]
                    ],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0] and "transport, passenger" in i[0]
                    ],
                )
            ] = (
                array[
                    self.array_inputs["battery cell production electricity"], :, index
                ].T
                * self.A[
                    :,
                    self.inputs[battery_cell_label],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0] and "transport, passenger" in i[0]
                    ],
                ]
            ).reshape(
                self.iterations, 1, -1
            )

        index_A = [
            self.inputs[c]
            for c in self.inputs
            if any(
                ele in c[0]
                for ele in ["ICEV-d", "ICEV-p", "HEV-p", "PHEV-p", "PHEV-d", "HEV-d"]
            )
        ]
        index = self.get_index_vehicle_from_array(
            ["ICEV-d", "ICEV-p", "HEV-p", "PHEV-p", "PHEV-d", "HEV-d"]
        )

        self.A[
            :,
            self.inputs[
                (
                    "polyethylene production, high density, granulate",
                    "RER",
                    "kilogram",
                    "polyethylene, high density, granulate",
                )
            ],
            index_A,
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            / array[self.array_inputs["lifetime kilometers"], :, index]
            * -1
        ).T

        index = self.get_index_vehicle_from_array("ICEV-g")
        self.A[
            :,
            self.inputs[
                (
                    "glass fibre reinforced plastic production, polyamide, injection moulded",
                    "RER",
                    "kilogram",
                    "glass fibre reinforced plastic, polyamide, injection moulded",
                )
            ],
            self.index_cng,
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            / array[self.array_inputs["lifetime kilometers"], :, index]
            * -1
        ).T

        if "hydrogen" in self.background_configuration["energy storage"]:
            # If a customization dict is passed
            hydro_tank_technology = self.background_configuration["energy storage"][
                "hydrogen"
            ]["type"]
        else:
            hydro_tank_technology = "carbon fiber"

        dict_tank_map = {
            "carbon fiber": (
                "Fuel tank, compressed hydrogen gas, 700bar",
                "GLO",
                "kilogram",
                "Fuel tank, compressed hydrogen gas, 700bar",
            ),
            "hdpe": (
                "Fuel tank, compressed hydrogen gas, 700bar, with HDPE liner",
                "RER",
                "kilogram",
                "Hydrogen tank",
            ),
            "aluminium": (
                "Fuel tank, compressed hydrogen gas, 700bar, with aluminium liner",
                "RER",
                "kilogram",
                "Hydrogen tank",
            ),
        }

        index = self.get_index_vehicle_from_array("FCEV")
        self.A[
            :, self.inputs[dict_tank_map[hydro_tank_technology]], self.index_fuel_cell,
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            / array[self.array_inputs["lifetime kilometers"], :, index]
            * -1
        ).T

        sum_renew, co2_intensity_tech = self.define_renewable_rate_in_mix()

        for y, year in enumerate(self.scope["year"]):

            if y + 1 == len(self.scope["year"]):
                end_str = "\n * "
            else:
                end_str = "\n \t * "

            print(
                "in "
                + str(year)
                + ", % of renewable: "
                + str(np.round(sum_renew[y] * 100, 0))
                + "%"
                + ", GHG intensity per kWh: "
                + str(int(np.sum(co2_intensity_tech[y] * self.mix[y])))
                + " g. CO2-eq.",
                end=end_str,
            )


        if any(
            True for x in ["BEV", "PHEV-p", "PHEV-d"] if x in self.scope["powertrain"]
        ):
            for y in self.scope["year"]:
                index = self.get_index_vehicle_from_array(
                    ["BEV", "PHEV-p", "PHEV-d"], y, method="and"
                )

                self.A[
                    np.ix_(
                        np.arange(self.iterations),
                        [
                            self.inputs[i]
                            for i in self.inputs
                            if str(y) in i[0]
                            and "electricity supply for electric vehicles" in i[0]
                        ],
                        [
                            self.inputs[i]
                            for i in self.inputs
                            if str(y) in i[0]
                            and "transport, passenger" in i[0]
                            and any(
                                True for x in ["BEV", "PHEV-p", "PHEV-d"] if x in i[0]
                            )
                        ],
                    )
                ] = (
                    array[self.array_inputs["electricity consumption"], :, index] * -1
                ).T.reshape(
                    self.iterations, 1, -1
                )

        if "FCEV" in self.scope["powertrain"]:

            index = self.get_index_vehicle_from_array("FCEV")

            if "tertiary" in self.fuel_blends["hydrogen"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["hydrogen"]["primary"]["type"],
                        self.fuel_blends["hydrogen"]["secondary"]["type"],
                        self.fuel_blends["hydrogen"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["hydrogen"]["primary"]["type"],
                        self.fuel_blends["hydrogen"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["hydrogen"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )


                # Fuel supply

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0] and "transport, passenger" in i[0] and "FCEV" in i[0]
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for hydrogen vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    array[self.array_inputs["fuel mass"], :, ind_array]
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if "ICEV-g" in self.scope["powertrain"]:
            index = self.get_index_vehicle_from_array("ICEV-g")

            if "tertiary" in self.fuel_blends["cng"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["cng"]["primary"]["type"],
                        self.fuel_blends["cng"]["secondary"]["type"],
                        self.fuel_blends["cng"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["cng"]["primary"]["type"],
                        self.fuel_blends["cng"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["cng"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

                # Primary fuel share

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0] and "transport, passenger" in i[0] and "ICEV-g" in i[0]
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Supply of gas, including 4% of gas input as pump-to-tank leakage
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0] and "fuel supply for gas vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (
                        array[self.array_inputs["fuel mass"], :, ind_array]
                        / array[self.array_inputs["range"], :, ind_array]
                    )
                    * (
                        1
                        + array[
                            self.array_inputs["CNG pump-to-tank leakage"], :, ind_array
                        ]
                    )
                    * -1
                ).T

                # Gas leakage to air

                self.A[
                    :, self.inputs[("Methane, fossil", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        array[self.array_inputs["fuel mass"], :, ind_array]
                        / array[self.array_inputs["range"], :, ind_array]
                    )
                    * array[self.array_inputs["CNG pump-to-tank leakage"], :, ind_array]
                    * -1
                ).T

                # Fuel-based emissions from CNG, CO2
                # The share and CO2 emissions factor of CNG is retrieved, if used

                share_fossil = 0
                CO2_fossil = 0

                if self.fuel_blends["cng"]["primary"]["type"] == "cng":
                    share_fossil += self.fuel_blends["cng"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["cng"]["primary"]["CO2"]

                if self.fuel_blends["cng"]["secondary"]["type"] == "cng":
                    share_fossil += self.fuel_blends["cng"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["cng"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["cng"]:
                    if self.fuel_blends["cng"]["tertiary"]["type"] == "cng":
                        share_fossil += self.fuel_blends["cng"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["cng"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    array[self.array_inputs["fuel mass"], :, ind_array]
                    * share_fossil
                    * CO2_fossil
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based CO2 emission from alternative CNG
                # The share of non-fossil gas in the blend is retrieved
                # As well as the CO2 emission factor of the fuel

                share_non_fossil = 0
                CO2_non_fossil = 0

                if self.fuel_blends["cng"]["primary"]["type"] != "cng":
                    share_non_fossil += self.fuel_blends["cng"]["primary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["cng"]["primary"]["CO2"]

                if self.fuel_blends["cng"]["secondary"]["type"] != "cng":
                    share_non_fossil += self.fuel_blends["cng"]["secondary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["cng"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["cng"]:
                    if self.fuel_blends["cng"]["tertiary"]["type"] != "cng":
                        share_non_fossil += self.fuel_blends["cng"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["cng"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if [i for i in self.scope["powertrain"] if i in ["ICEV-d", "PHEV-d", "HEV-d"]]:
            index = self.get_index_vehicle_from_array(["ICEV-d", "PHEV-d", "HEV-d"])

            if "tertiary" in self.fuel_blends["diesel"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["diesel"]["primary"]["type"],
                        self.fuel_blends["diesel"]["secondary"]["type"],
                        self.fuel_blends["diesel"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["diesel"]["primary"]["type"],
                        self.fuel_blends["diesel"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["diesel"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "transport, passenger" in i[0]
                    and any(x in i[0] for x in ["ICEV-d", "PHEV-d", "HEV-d"])
                ]

                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Fuel supply
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for diesel vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (array[self.array_inputs["fuel mass"], :, ind_array])
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_fossil = 0
                CO2_fossil = 0
                # Fuel-based CO2 emission from conventional diesel
                if self.fuel_blends["diesel"]["primary"]["type"] == "diesel":
                    share_fossil += self.fuel_blends["diesel"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["diesel"]["primary"]["CO2"]

                if self.fuel_blends["diesel"]["secondary"]["type"] == "diesel":
                    share_fossil += self.fuel_blends["diesel"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["diesel"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["diesel"]:
                    if self.fuel_blends["diesel"]["tertiary"]["type"] == "diesel":
                        share_fossil += self.fuel_blends["diesel"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["diesel"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                            * CO2_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based SO2 emissions
                # Sulfur concentration value for a given country, a given year, as concentration ratio
                if self.country in self.bs.sulfur.country.values:
                    sulfur_concentration = (
                        self.bs.sulfur.sel(
                            country=self.country, year=year, fuel="diesel"
                        )
                        .sum()
                        .values
                    )
                else:
                    d_map_regions = {
                        "CAZ": "CA",
                        "CHA": "CN",
                        "EUR": "FR",
                        "IND": "IN",
                        "JPN": "JP",
                        "LAM": "BR",
                        "MEA": "EG",
                        "NEU": "CH",
                        "OAS": "TH",
                        "REF": "RU",
                        "SSA": "ZA",
                        "USA": "US",
                        "World": "RER",
                    }

                    if self.country in d_map_regions:

                        sulfur_concentration = (
                            self.bs.sulfur.sel(
                                country=d_map_regions[self.country],
                                year=year,
                                fuel="diesel",
                            )
                            .sum()
                            .values
                        )
                    else:

                        # if we do not have the sulfur concentration for the required country, we pick Europe
                        print(
                            "The sulfur content for diesel fuel in {} could not be found. European average sulfur content is used instead.".format(
                                self.country
                            )
                        )
                        sulfur_concentration = (
                            self.bs.sulfur.sel(country="RER", year=year, fuel="diesel")
                            .sum()
                            .values
                        )

                self.A[
                    :, self.inputs[("Sulfur dioxide", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil  # assumes sulfur only present in conventional diesel
                            * sulfur_concentration
                            * (64 / 32)  # molar mass of SO2/molar mass of O2
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_non_fossil = 0
                CO2_non_fossil = 0

                # Fuel-based CO2 emission from alternative petrol
                # The share of non-fossil fuel in the blend is retrieved
                # As well as the CO2 emission factor of the fuel
                if self.fuel_blends["diesel"]["primary"]["type"] != "diesel":
                    share_non_fossil += self.fuel_blends["diesel"]["primary"]["share"][
                        y
                    ]
                    CO2_non_fossil = self.fuel_blends["diesel"]["primary"]["CO2"]

                if self.fuel_blends["diesel"]["secondary"]["type"] != "diesel":
                    share_non_fossil += self.fuel_blends["diesel"]["secondary"][
                        "share"
                    ][y]
                    CO2_non_fossil = self.fuel_blends["diesel"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["diesel"]:
                    if self.fuel_blends["diesel"]["tertiary"]["type"] != "diesel":
                        share_non_fossil += self.fuel_blends["diesel"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["diesel"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Heavy metals emissions from conventional diesel
                # Emission factors from Spielmann et al., Transport Services Data v.2 (2007)
                # Cadmium, 0.01 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Cadmium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Copper, 1.7 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Copper", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.7e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium, 0.05 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Chromium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 5.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Nickel, 0.07 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Nickel", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 7.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Selenium, 0.01 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Selenium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Zinc, 1 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Zinc", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium VI, 0.0001 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        (
                            "Chromium VI",
                            ("air", "urban air close to ground"),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-10
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if [i for i in self.scope["powertrain"] if i in ["ICEV-p", "HEV-p", "PHEV-p"]]:
            index = self.get_index_vehicle_from_array(["ICEV-p", "HEV-p", "PHEV-p"])

            if "tertiary" in self.fuel_blends["petrol"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["petrol"]["primary"]["type"],
                        self.fuel_blends["petrol"]["secondary"]["type"],
                        self.fuel_blends["petrol"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["petrol"]["primary"]["type"],
                        self.fuel_blends["petrol"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["petrol"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

            for y, year in enumerate(self.scope["year"]):
                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "transport, passenger" in i[0]
                    and any(x in i[0] for x in ["ICEV-p", "HEV-p", "PHEV-p"])
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Fuel supply
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for gasoline vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (array[self.array_inputs["fuel mass"], :, ind_array])
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_fossil = 0
                CO2_fossil = 0

                # Fuel-based CO2 emission from conventional petrol
                if self.fuel_blends["petrol"]["primary"]["type"] == "petrol":
                    share_fossil += self.fuel_blends["petrol"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["petrol"]["primary"]["CO2"]

                if self.fuel_blends["petrol"]["secondary"]["type"] == "petrol":
                    share_fossil += self.fuel_blends["petrol"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["petrol"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["petrol"]:
                    if self.fuel_blends["petrol"]["tertiary"]["type"] == "petrol":
                        share_fossil += self.fuel_blends["petrol"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["petrol"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                            * CO2_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based SO2 emissions
                # Sulfur concentration value for a given country, a given year, as a concentration ratio

                if self.country in self.bs.sulfur.country.values:
                    sulfur_concentration = (
                        self.bs.sulfur.sel(
                            country=self.country, year=year, fuel="petrol"
                        )
                        .sum()
                        .values
                    )
                else:
                    d_map_regions = {
                        "CAZ": "CA",
                        "CHA": "CN",
                        "EUR": "FR",
                        "IND": "IN",
                        "JPN": "JP",
                        "LAM": "BR",
                        "MEA": "EG",
                        "NEU": "CH",
                        "OAS": "TH",
                        "REF": "RU",
                        "SSA": "ZA",
                        "USA": "US",
                        "World": "RER",
                    }

                    if self.country in d_map_regions:

                        sulfur_concentration = (
                            self.bs.sulfur.sel(
                                country=d_map_regions[self.country],
                                year=year,
                                fuel="diesel",
                            )
                            .sum()
                            .values
                        )
                    else:
                        # if we do not have the sulfur concentration for the required country, we pick Europe
                        print(
                            "The sulfur content for gasoline fuel in {} could not be found. European average sulfur content is used instead.".format(
                                self.country
                            )
                        )
                        sulfur_concentration = (
                            self.bs.sulfur.sel(country="RER", year=year, fuel="petrol")
                            .sum()
                            .values
                        )

                self.A[
                    :, self.inputs[("Sulfur dioxide", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil  # assumes sulfur only present in conventional diesel
                            * sulfur_concentration
                            * (64 / 32)  # molar mass of SO2/molar mass of O2
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_non_fossil = 0
                CO2_non_fossil = 0

                # Fuel-based CO2 emission from alternative petrol
                # The share of non-fossil fuel in the blend is retrieved
                # As well as the CO2 emission factor of the fuel
                if self.fuel_blends["petrol"]["primary"]["type"] != "petrol":
                    share_non_fossil += self.fuel_blends["petrol"]["primary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["petrol"]["primary"]["CO2"]

                if self.fuel_blends["petrol"]["secondary"]["type"] != "petrol":
                    share_non_fossil += self.fuel_blends["petrol"]["secondary"][
                        "share"
                    ][y]
                    CO2_non_fossil = self.fuel_blends["petrol"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["petrol"]:
                    if self.fuel_blends["petrol"]["tertiary"]["type"] != "petrol":
                        share_non_fossil += self.fuel_blends["petrol"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["petrol"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Heavy metals emissions from conventional petrol
                # Cadmium, 0.01 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Cadmium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Copper, 1.7 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Copper", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.7e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium, 0.05 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Chromium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 5.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Nickel, 0.07 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Nickel", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 7.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Selenium, 0.01 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Selenium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Zinc, 1 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Zinc", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium VI, 0.0001 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        (
                            "Chromium VI",
                            ("air", "urban air close to ground"),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-10
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        # Non-exhaust emissions
        self.A[
            :,
            self.inputs[
                (
                    "market for road wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "road wear emissions, passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (array[self.array_inputs["driving mass"], :] * 1e-08)
        self.A[
            :,
            self.inputs[
                (
                    "market for tyre wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "tyre wear emissions, passenger car",
                )
            ],
            -self.number_of_cars :,
        ] = (array[self.array_inputs["driving mass"], :] * 6e-08)

        # Brake wear emissions
        # BEVs only emit 20% of what a combustion engine vehicle emit according to
        # https://link.springer.com/article/10.1007/s11367-014-0792-4
        ind_A = [
            self.inputs[i]
            for i in self.inputs
            if "transport, passenger" in i[0]
            and any(x in i[0] for x in ["ICEV-d", "ICEV-p", "ICEV-g"])
        ]

        index = self.get_index_vehicle_from_array(["ICEV-d", "ICEV-p", "ICEV-g"])

        self.A[
            :,
            self.inputs[
                (
                    "market for brake wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "brake wear emissions, passenger car",
                )
            ],
            ind_A,
        ] = (array[self.array_inputs["driving mass"], :, index].T * 5e-09)

        ind_A = [
            self.inputs[i]
            for i in self.inputs
            if "transport, passenger" in i[0]
            and any(
                x in i[0] for x in ["BEV", "FCEV", "HEV-p", "HEV-d", "PHEV-p", "PHEV-d"]
            )
        ]

        index = self.get_index_vehicle_from_array(
            ["BEV", "FCEV", "HEV-p", "HEV-d", "PHEV-p", "PHEV-d"]
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for brake wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "brake wear emissions, passenger car",
                )
            ],
            ind_A,
        ] = (array[self.array_inputs["driving mass"], :, index].T * 5e-09 * 0.2)

        # Infrastructure
        self.A[
            :,
            self.inputs[("market for road", "GLO", "meter-year", "road")],
            -self.number_of_cars :,
        ] = (5.37e-7 * array[self.array_inputs["driving mass"], :] * -1)

        # Infrastructure maintenance
        self.A[
            :,
            self.inputs[
                ("market for road maintenance", "RER", "meter-year", "road maintenance")
            ],
            -self.number_of_cars :,
        ] = (1.29e-3 * -1)

        # Exhaust emissions
        # Non-fuel based emissions
        self.A[:, self.index_emissions, -self.number_of_cars :] = (
            array[
                [
                    self.array_inputs[self.map_non_fuel_emissions[self.rev_inputs[x]]]
                    for x in self.index_emissions
                ]
            ]
            * -1
        ).transpose([1, 0, 2])

        # Noise emissions
        self.A[:, self.index_noise, -self.number_of_cars :] = (
            array[
                [
                    self.array_inputs[self.map_noise_emissions[self.rev_inputs[x]]]
                    for x in self.index_noise
                ]
            ]
            * -1
        ).transpose([1, 0, 2])

        # Emissions of air conditioner refrigerant r134a
        # Leakage assumed to amount to 53g according to
        # https://ec.europa.eu/clima/sites/clima/files/eccp/docs/leakage_rates_final_report_en.pdf

        self.A[
            :,
            self.inputs[
                (
                    "Ethane, 1,1,1,2-tetrafluoro-, HFC-134a",
                    ("air",),
                    "kilogram"
                )
            ],
            -self.number_of_cars:
        ] = .053 / self.array.values[self.array_inputs["kilometers per year"]] * -1

        self.A[
            :,
            self.inputs[
                (
                    "market for refrigerant R134a",
                    "GLO",
                    "kilogram",
                    "refrigerant R134a"
                )
            ],
            -self.number_of_cars:
        ] = .053 / self.array.values[self.array_inputs["kilometers per year"]] * -1

        print("*********************************************************************")

    def set_inputs_in_A_matrix_for_export(self, array):
        """
        Fill-in the A matrix. Does not return anything. Modifies in place.
        Shape of the A matrix (values, products, activities).

        :param array: :attr:`array` from :class:`CarModel` class
        """

        # Glider
        self.A[
            :,
            self.inputs[
                (
                    "market for glider, passenger car",
                    "GLO",
                    "kilogram",
                    "glider, passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ],
        ] = (
            (array[self.array_inputs["glider base mass"], :])
            * -1
        )

        self.A[
            :,
            self.inputs[
                ("Glider lightweighting", "GLO", "kilogram", "Glider lightweighting")
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            (
                array[self.array_inputs["lightweighting"], :]
                * array[self.array_inputs["glider base mass"], :]
            )
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "maintenance, passenger car",
                    "RER",
                    "unit",
                    "passenger car maintenance",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "transport, passenger car" in i[0]
            ]
        ] = (array[self.array_inputs["curb mass"], :] / 1240 / 150000 * -1)

        # Glider EoL
        self.A[
            :,
            self.inputs[
                (
                    "market for manual dismantling of used electric passenger car",
                    "GLO",
                    "unit",
                    "manual dismantling of used electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["curb mass"], :]
            * (1 - array[self.array_inputs["combustion power share"], :])
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for manual dismantling of used passenger car with internal combustion engine",
                    "GLO",
                    "unit",
                    "manual dismantling of used passenger car with internal combustion engine",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["curb mass"], :]
            * array[self.array_inputs["combustion power share"], :]
            * -1
        )

        # Powertrain components
        self.A[
            :,
            self.inputs[
                (
                    "market for charger, electric passenger car",
                    "GLO",
                    "kilogram",
                    "charger, electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["charger mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for converter, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "converter, for electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["converter mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for electric motor, electric passenger car",
                    "GLO",
                    "kilogram",
                    "electric motor, electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["electric engine mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for inverter, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "inverter, for electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["inverter mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for power distribution unit, for electric passenger car",
                    "GLO",
                    "kilogram",
                    "power distribution unit, for electric passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["power distribution unit mass"], :]
            * -1
        )

        l_elec_pt = [
            "charger mass",
            "converter mass",
            "inverter mass",
            "power distribution unit mass",
            "electric engine mass",
            "fuel cell stack mass",
            "fuel cell ancillary BoP mass",
            "fuel cell essential BoP mass",
            "battery cell mass",
            "battery BoP mass",
        ]

        self.A[
            :,
            self.inputs[
                (
                    "market for used powertrain from electric passenger car, manual dismantling",
                    "GLO",
                    "kilogram",
                    "used powertrain from electric passenger car, manual dismantling",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[[self.array_inputs[l] for l in l_elec_pt], :].sum(axis=0)
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for internal combustion engine, passenger car",
                    "GLO",
                    "kilogram",
                    "internal combustion engine, for passenger car",
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            (
                array[
                    [
                        self.array_inputs[l]
                        for l in ["combustion engine mass", "powertrain mass"]
                    ],
                    :,
                ].sum(axis=0)
            )
            * -1
        )

        self.A[
            :,
            self.inputs[("Ancillary BoP", "GLO", "kilogram", "Ancillary BoP")],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["fuel cell ancillary BoP mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[("Essential BoP", "GLO", "kilogram", "Essential BoP")],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["fuel cell essential BoP mass"], :]
            * -1
        )

        self.A[
            :,
            self.inputs[("Stack", "GLO", "kilowatt", "Stack")],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            array[self.array_inputs["fuel cell stack mass"], :]
            * -1
        )

        # Start of printout

        print(
            "****************** IMPORTANT BACKGROUND PARAMETERS ******************",
            end="\n * ",
        )

        # Energy storage

        print(
            "The country of use is " + self.country, end="\n * ",
        )

        battery_tech = self.background_configuration["energy storage"]["electric"][
            "type"
        ]
        battery_origin = self.background_configuration["energy storage"]["electric"][
            "origin"
        ]

        print(
            "Power and energy batteries produced in "
            + battery_origin
            + " using "
            + battery_tech
            + " chemistry.",
            end="\n * ",
        )

        # Use the NMC inventory of Schmidt et al. 2019
        self.A[
            :,
            self.inputs[("Battery BoP", "GLO", "kilogram", "Battery BoP")],
            [
                self.inputs[i]
                for i in self.inputs
                if "Passenger car" in i[0]
            ]
        ] = (
            (
                array[self.array_inputs["battery BoP mass"], :]
                * (1 + array[self.array_inputs["battery lifetime replacements"], :])
            )
            * -1
        )

        battery_cell_label = (
            "Battery cell, " + battery_tech,
            "GLO",
            "kilogram",
            "Battery cell",
        )

        self.A[:,
               self.inputs[battery_cell_label],
                [
                    self.inputs[i]
                    for i in self.inputs
                    if "Passenger car" in i[0]
                ]
        ] = (
            (
                array[self.array_inputs["battery cell mass"], :]
                * (1 + array[self.array_inputs["fuel cell lifetime replacements"], :])
            )
            * -1
        )

        # Set an input of electricity, given the country of manufacture
        self.A[
            :,
            self.inputs[
                (
                    "market group for electricity, medium voltage",
                    "World",
                    "kilowatt hour",
                    "electricity, medium voltage",
                )
            ],
            self.inputs[battery_cell_label],
        ] = 0

        for y in self.scope["year"]:
            index = self.get_index_vehicle_from_array(y)

            self.A[
                np.ix_(
                    np.arange(self.iterations),
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0]
                        and "electricity market for energy storage production" in i[0]
                    ],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0] and "Passenger car" in i[0]
                    ],
                )
            ] = (
                array[
                    self.array_inputs["battery cell production electricity"], :, index
                ].T
                * self.A[
                    :,
                    self.inputs[battery_cell_label],
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(y) in i[0] and "Passenger car" in i[0]
                    ],
                ]
            ).reshape(
                self.iterations, 1, -1
            )

        index_A = [
            self.inputs[c]
            for c in self.inputs
            if any(
                ele in c[0]
                for ele in ["ICEV-d", "ICEV-p", "HEV-p", "PHEV-p", "PHEV-d", "HEV-d"]
            ) and "Passenger car" in c[0]
        ]
        index = self.get_index_vehicle_from_array(
            ["ICEV-d", "ICEV-p", "HEV-p", "PHEV-p", "PHEV-d", "HEV-d"]
        )

        self.A[
            :,
            self.inputs[
                (
                    "polyethylene production, high density, granulate",
                    "RER",
                    "kilogram",
                    "polyethylene, high density, granulate",
                )
            ],
            index_A,
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            * -1
        ).T

        index_A = [
            self.inputs[c]
            for c in self.inputs
            if "ICEV-g" in c[0] and "Passenger car" in c[0]
        ]

        index = self.get_index_vehicle_from_array("ICEV-g")
        self.A[
            :,
            self.inputs[
                (
                    "glass fibre reinforced plastic production, polyamide, injection moulded",
                    "RER",
                    "kilogram",
                    "glass fibre reinforced plastic, polyamide, injection moulded",
                )
            ],
            index_A,
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            * -1
        ).T

        if "hydrogen" in self.background_configuration["energy storage"]:
            # If a customization dict is passed
            hydro_tank_technology = self.background_configuration["energy storage"][
                "hydrogen"
            ]["type"]
        else:
            hydro_tank_technology = "carbon fiber"

        dict_tank_map = {
            "carbon fiber": (
                "Fuel tank, compressed hydrogen gas, 700bar",
                "GLO",
                "kilogram",
                "Fuel tank, compressed hydrogen gas, 700bar",
            ),
            "hdpe": (
                "Fuel tank, compressed hydrogen gas, 700bar, with HDPE liner",
                "RER",
                "kilogram",
                "Hydrogen tank",
            ),
            "aluminium": (
                "Fuel tank, compressed hydrogen gas, 700bar, with aluminium liner",
                "RER",
                "kilogram",
                "Hydrogen tank",
            ),
        }

        index_A = [
            self.inputs[c]
            for c in self.inputs
            if "FCEV" in c[0] and "Passenger car" in c[0]
        ]

        index = self.get_index_vehicle_from_array("FCEV")
        self.A[
            :, self.inputs[dict_tank_map[hydro_tank_technology]], index_A
        ] = (
            array[self.array_inputs["fuel tank mass"], :, index]
            * -1
        ).T


        # END of vehicle building

        self.A[
            :,
            [self.inputs[c]
            for c in self.inputs
            if "Passenger car" in c[0]],
            [self.inputs[c] for c in self.inputs
             if "transport, passenger car" in c[0]]
            ] = -1/ array[self.array_inputs["lifetime kilometers"]]

        sum_renew, co2_intensity_tech = self.define_renewable_rate_in_mix()

        for y, year in enumerate(self.scope["year"]):

            if y + 1 == len(self.scope["year"]):
                end_str = "\n * "
            else:
                end_str = "\n \t * "

            print(
                "in "
                + str(year)
                + ", % of renewable: "
                + str(np.round(sum_renew[y] * 100, 0))
                + "%"
                + ", GHG intensity per kWh: "
                + str(int(np.sum(co2_intensity_tech[y] * self.mix[y])))
                + " g. CO2-eq.",
                end=end_str,
            )


        if any(
            True for x in ["BEV", "PHEV-p", "PHEV-d"] if x in self.scope["powertrain"]
        ):
            for y in self.scope["year"]:
                index = self.get_index_vehicle_from_array(
                    ["BEV", "PHEV-p", "PHEV-d"], y, method="and"
                )

                self.A[
                    np.ix_(
                        np.arange(self.iterations),
                        [
                            self.inputs[i]
                            for i in self.inputs
                            if str(y) in i[0]
                            and "electricity supply for electric vehicles" in i[0]
                        ],
                        [
                            self.inputs[i]
                            for i in self.inputs
                            if str(y) in i[0]
                            and "transport, passenger" in i[0]
                            and any(
                                True for x in ["BEV", "PHEV-p", "PHEV-d"] if x in i[0]
                            )
                        ],
                    )
                ] = (
                    array[self.array_inputs["electricity consumption"], :, index] * -1
                ).T.reshape(
                    self.iterations, 1, -1
                )

        if "FCEV" in self.scope["powertrain"]:

            index = self.get_index_vehicle_from_array("FCEV")

            if "tertiary" in self.fuel_blends["hydrogen"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["hydrogen"]["primary"]["type"],
                        self.fuel_blends["hydrogen"]["secondary"]["type"],
                        self.fuel_blends["hydrogen"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["hydrogen"]["primary"]["type"],
                        self.fuel_blends["hydrogen"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["hydrogen"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["hydrogen"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )


                # Fuel supply

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0] and "transport, passenger" in i[0] and "FCEV" in i[0]
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for hydrogen vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    array[self.array_inputs["fuel mass"], :, ind_array]
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if "ICEV-g" in self.scope["powertrain"]:
            index = self.get_index_vehicle_from_array("ICEV-g")

            if "tertiary" in self.fuel_blends["cng"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["cng"]["primary"]["type"],
                        self.fuel_blends["cng"]["secondary"]["type"],
                        self.fuel_blends["cng"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["cng"]["primary"]["type"],
                        self.fuel_blends["cng"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["cng"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["cng"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

                # Primary fuel share

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0] and "transport, passenger" in i[0] and "ICEV-g" in i[0]
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Supply of gas, including 4% of gas input as pump-to-tank leakage
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0] and "fuel supply for gas vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (
                        array[self.array_inputs["fuel mass"], :, ind_array]
                        / array[self.array_inputs["range"], :, ind_array]
                    )
                    * (
                        1
                        + array[
                            self.array_inputs["CNG pump-to-tank leakage"], :, ind_array
                        ]
                    )
                    * -1
                ).T

                # Gas leakage to air

                self.A[
                    :, self.inputs[("Methane, fossil", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        array[self.array_inputs["fuel mass"], :, ind_array]
                        / array[self.array_inputs["range"], :, ind_array]
                    )
                    * array[self.array_inputs["CNG pump-to-tank leakage"], :, ind_array]
                    * -1
                ).T

                # Fuel-based emissions from CNG, CO2
                # The share and CO2 emissions factor of CNG is retrieved, if used

                share_fossil = 0
                CO2_fossil = 0

                if self.fuel_blends["cng"]["primary"]["type"] == "cng":
                    share_fossil += self.fuel_blends["cng"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["cng"]["primary"]["CO2"]

                if self.fuel_blends["cng"]["secondary"]["type"] == "cng":
                    share_fossil += self.fuel_blends["cng"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["cng"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["cng"]:
                    if self.fuel_blends["cng"]["tertiary"]["type"] == "cng":
                        share_fossil += self.fuel_blends["cng"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["cng"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    array[self.array_inputs["fuel mass"], :, ind_array]
                    * share_fossil
                    * CO2_fossil
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based CO2 emission from alternative CNG
                # The share of non-fossil gas in the blend is retrieved
                # As well as the CO2 emission factor of the fuel

                share_non_fossil = 0
                CO2_non_fossil = 0

                if self.fuel_blends["cng"]["primary"]["type"] != "cng":
                    share_non_fossil += self.fuel_blends["cng"]["primary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["cng"]["primary"]["CO2"]

                if self.fuel_blends["cng"]["secondary"]["type"] != "cng":
                    share_non_fossil += self.fuel_blends["cng"]["secondary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["cng"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["cng"]:
                    if self.fuel_blends["cng"]["tertiary"]["type"] != "cng":
                        share_non_fossil += self.fuel_blends["cng"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["cng"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if [i for i in self.scope["powertrain"] if i in ["ICEV-d", "PHEV-d", "HEV-d"]]:
            index = self.get_index_vehicle_from_array(["ICEV-d", "PHEV-d", "HEV-d"])

            if "tertiary" in self.fuel_blends["diesel"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["diesel"]["primary"]["type"],
                        self.fuel_blends["diesel"]["secondary"]["type"],
                        self.fuel_blends["diesel"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["diesel"]["primary"]["type"],
                        self.fuel_blends["diesel"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["diesel"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["diesel"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "transport, passenger car" in i[0]
                    and any(x in i[0] for x in ["ICEV-d", "PHEV-d", "HEV-d"])
                ]

                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Fuel supply
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for diesel vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (array[self.array_inputs["fuel mass"], :, ind_array])
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_fossil = 0
                CO2_fossil = 0
                # Fuel-based CO2 emission from conventional diesel
                if self.fuel_blends["diesel"]["primary"]["type"] == "diesel":
                    share_fossil += self.fuel_blends["diesel"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["diesel"]["primary"]["CO2"]

                if self.fuel_blends["diesel"]["secondary"]["type"] == "diesel":
                    share_fossil += self.fuel_blends["diesel"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["diesel"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["diesel"]:
                    if self.fuel_blends["diesel"]["tertiary"]["type"] == "diesel":
                        share_fossil += self.fuel_blends["diesel"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["diesel"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                            * CO2_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based SO2 emissions
                # Sulfur concentration value for a given country, a given year, as concentration ratio
                if self.country in self.bs.sulfur.country.values:
                    sulfur_concentration = (
                        self.bs.sulfur.sel(
                            country=self.country, year=year, fuel="diesel"
                        )
                        .sum()
                        .values
                    )
                else:
                    d_map_regions = {
                        "CAZ": "CA",
                        "CHA": "CN",
                        "EUR": "FR",
                        "IND": "IN",
                        "JPN": "JP",
                        "LAM": "BR",
                        "MEA": "EG",
                        "NEU": "CH",
                        "OAS": "TH",
                        "REF": "RU",
                        "SSA": "ZA",
                        "USA": "US",
                        "World": "RER",
                    }

                    if self.country in d_map_regions:

                        sulfur_concentration = (
                            self.bs.sulfur.sel(
                                country=d_map_regions[self.country],
                                year=year,
                                fuel="diesel",
                            )
                            .sum()
                            .values
                        )
                    else:

                        # if we do not have the sulfur concentration for the required country, we pick Europe
                        print(
                            "The sulfur content for diesel fuel in {} could not be found. European average sulfur content is used instead.".format(
                                self.country
                            )
                        )
                        sulfur_concentration = (
                            self.bs.sulfur.sel(country="RER", year=year, fuel="diesel")
                            .sum()
                            .values
                        )

                self.A[
                    :, self.inputs[("Sulfur dioxide", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil  # assumes sulfur only present in conventional diesel
                            * sulfur_concentration
                            * (64 / 32)  # molar mass of SO2/molar mass of O2
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_non_fossil = 0
                CO2_non_fossil = 0

                # Fuel-based CO2 emission from alternative petrol
                # The share of non-fossil fuel in the blend is retrieved
                # As well as the CO2 emission factor of the fuel
                if self.fuel_blends["diesel"]["primary"]["type"] != "diesel":
                    share_non_fossil += self.fuel_blends["diesel"]["primary"]["share"][
                        y
                    ]
                    CO2_non_fossil = self.fuel_blends["diesel"]["primary"]["CO2"]

                if self.fuel_blends["diesel"]["secondary"]["type"] != "diesel":
                    share_non_fossil += self.fuel_blends["diesel"]["secondary"][
                        "share"
                    ][y]
                    CO2_non_fossil = self.fuel_blends["diesel"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["diesel"]:
                    if self.fuel_blends["diesel"]["tertiary"]["type"] != "diesel":
                        share_non_fossil += self.fuel_blends["diesel"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["diesel"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Heavy metals emissions from conventional diesel
                # Emission factors from Spielmann et al., Transport Services Data v.2 (2007)
                # Cadmium, 0.01 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Cadmium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Copper, 1.7 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Copper", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.7e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium, 0.05 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Chromium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 5.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Nickel, 0.07 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Nickel", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 7.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Selenium, 0.01 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Selenium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Zinc, 1 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        ("Zinc", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium VI, 0.0001 mg/kg diesel
                self.A[
                    :,
                    self.inputs[
                        (
                            "Chromium VI",
                            ("air", "urban air close to ground"),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-10
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        if [i for i in self.scope["powertrain"] if i in ["ICEV-p", "HEV-p", "PHEV-p"]]:
            index = self.get_index_vehicle_from_array(["ICEV-p", "HEV-p", "PHEV-p"])

            if "tertiary" in self.fuel_blends["petrol"]:
                print(
                    "{} is completed by {} and {}.".format(
                        self.fuel_blends["petrol"]["primary"]["type"],
                        self.fuel_blends["petrol"]["secondary"]["type"],
                        self.fuel_blends["petrol"]["tertiary"]["type"],
                    ),
                    end="\n \t * ",
                )

            else:

                print(
                    "{} is completed by {}.".format(
                        self.fuel_blends["petrol"]["primary"]["type"],
                        self.fuel_blends["petrol"]["secondary"]["type"],
                    ),
                    end="\n \t * ",
                )

            for y, year in enumerate(self.scope["year"]):
                if y + 1 == len(self.scope["year"]):
                    end_str = "\n * "
                else:
                    end_str = "\n \t * "

                if "tertiary" in self.fuel_blends["petrol"]:
                    print(
                        "in "
                        + str(year)
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%"
                        + " _________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["tertiary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )
                else:
                    print(
                        "in "
                        + str(year)
                        + " _________________________________________ "
                        + str(
                            np.round(
                                self.fuel_blends["petrol"]["secondary"]["share"][y] * 100,
                                0,
                            )
                        )
                        + "%",
                        end=end_str,
                    )

            for y, year in enumerate(self.scope["year"]):
                ind_A = [
                    self.inputs[i]
                    for i in self.inputs
                    if str(year) in i[0]
                    and "transport, passenger" in i[0]
                    and any(x in i[0] for x in ["ICEV-p", "HEV-p", "PHEV-p"])
                ]
                ind_array = [
                    x for x in self.get_index_vehicle_from_array(year) if x in index
                ]

                # Fuel supply
                self.A[
                    :,
                    [
                        self.inputs[i]
                        for i in self.inputs
                        if str(year) in i[0]
                        and "fuel supply for gasoline vehicles" in i[0]
                    ],
                    ind_A,
                ] = (
                    (array[self.array_inputs["fuel mass"], :, ind_array])
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_fossil = 0
                CO2_fossil = 0

                # Fuel-based CO2 emission from conventional petrol
                if self.fuel_blends["petrol"]["primary"]["type"] == "petrol":
                    share_fossil += self.fuel_blends["petrol"]["primary"]["share"][y]
                    CO2_fossil = self.fuel_blends["petrol"]["primary"]["CO2"]

                if self.fuel_blends["petrol"]["secondary"]["type"] == "petrol":
                    share_fossil += self.fuel_blends["petrol"]["secondary"]["share"][y]
                    CO2_fossil = self.fuel_blends["petrol"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["petrol"]:
                    if self.fuel_blends["petrol"]["tertiary"]["type"] == "petrol":
                        share_fossil += self.fuel_blends["petrol"]["tertiary"]["share"][y]
                        CO2_fossil = self.fuel_blends["petrol"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[("Carbon dioxide, fossil", ("air",), "kilogram")],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                            * CO2_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Fuel-based SO2 emissions
                # Sulfur concentration value for a given country, a given year, as a concentration ratio

                if self.country in self.bs.sulfur.country.values:
                    sulfur_concentration = (
                        self.bs.sulfur.sel(
                            country=self.country, year=year, fuel="petrol"
                        )
                        .sum()
                        .values
                    )
                else:
                    d_map_regions = {
                        "CAZ": "CA",
                        "CHA": "CN",
                        "EUR": "FR",
                        "IND": "IN",
                        "JPN": "JP",
                        "LAM": "BR",
                        "MEA": "EG",
                        "NEU": "CH",
                        "OAS": "TH",
                        "REF": "RU",
                        "SSA": "ZA",
                        "USA": "US",
                        "World": "RER",
                    }

                    if self.country in d_map_regions:

                        sulfur_concentration = (
                            self.bs.sulfur.sel(
                                country=d_map_regions[self.country],
                                year=year,
                                fuel="diesel",
                            )
                            .sum()
                            .values
                        )
                    else:
                        # if we do not have the sulfur concentration for the required country, we pick Europe
                        print(
                            "The sulfur content for gasoline fuel in {} could not be found. European average sulfur content is used instead.".format(
                                self.country
                            )
                        )
                        sulfur_concentration = (
                            self.bs.sulfur.sel(country="RER", year=year, fuel="petrol")
                            .sum()
                            .values
                        )

                self.A[
                    :, self.inputs[("Sulfur dioxide", ("air",), "kilogram")], ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil  # assumes sulfur only present in conventional diesel
                            * sulfur_concentration
                            * (64 / 32)  # molar mass of SO2/molar mass of O2
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                share_non_fossil = 0
                CO2_non_fossil = 0

                # Fuel-based CO2 emission from alternative petrol
                # The share of non-fossil fuel in the blend is retrieved
                # As well as the CO2 emission factor of the fuel
                if self.fuel_blends["petrol"]["primary"]["type"] != "petrol":
                    share_non_fossil += self.fuel_blends["petrol"]["primary"]["share"][y]
                    CO2_non_fossil = self.fuel_blends["petrol"]["primary"]["CO2"]

                if self.fuel_blends["petrol"]["secondary"]["type"] != "petrol":
                    share_non_fossil += self.fuel_blends["petrol"]["secondary"][
                        "share"
                    ][y]
                    CO2_non_fossil = self.fuel_blends["petrol"]["secondary"]["CO2"]

                if "tertiary" in self.fuel_blends["petrol"]:
                    if self.fuel_blends["petrol"]["tertiary"]["type"] != "petrol":
                        share_non_fossil += self.fuel_blends["petrol"]["tertiary"]["share"][y]
                        CO2_non_fossil = self.fuel_blends["petrol"]["tertiary"]["CO2"]

                self.A[
                    :,
                    self.inputs[
                        (
                            "Carbon dioxide, from soil or biomass stock",
                            ("air",),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_non_fossil
                            * CO2_non_fossil
                        )
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Heavy metals emissions from conventional petrol
                # Cadmium, 0.01 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Cadmium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Copper, 1.7 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Copper", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.7e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium, 0.05 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Chromium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 5.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Nickel, 0.07 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Nickel", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 7.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Selenium, 0.01 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Selenium", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-8
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Zinc, 1 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        ("Zinc", ("air", "urban air close to ground"), "kilogram")
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-6
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

                # Chromium VI, 0.0001 mg/kg gasoline
                self.A[
                    :,
                    self.inputs[
                        (
                            "Chromium VI",
                            ("air", "urban air close to ground"),
                            "kilogram",
                        )
                    ],
                    ind_A,
                ] = (
                    (
                        (
                            array[self.array_inputs["fuel mass"], :, ind_array]
                            * share_fossil
                        )
                        * 1.0e-10
                    )
                    / array[self.array_inputs["range"], :, ind_array]
                    * -1
                ).T

        # Non-exhaust emissions
        ind_A = [
            self.inputs[i]
            for i in self.inputs
            if "transport, passenger" in i[0]
        ]
        self.A[
            :,
            self.inputs[
                (
                    "market for road wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "road wear emissions, passenger car",
                )
            ],
            ind_A
        ] = (array[self.array_inputs["driving mass"], :] * 1e-08)

        self.A[
            :,
            self.inputs[
                (
                    "market for tyre wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "tyre wear emissions, passenger car",
                )
            ],
            ind_A
        ] = (array[self.array_inputs["driving mass"], :] * 6e-08)

        # Brake wear emissions
        # BEVs only emit 20% of what a combustion engine vehicle emit according to
        # https://link.springer.com/article/10.1007/s11367-014-0792-4
        ind_A = [
            self.inputs[i]
            for i in self.inputs
            if "transport, passenger" in i[0]
            and any(x in i[0] for x in ["ICEV-d", "ICEV-p", "ICEV-g"])
        ]

        index = self.get_index_vehicle_from_array(["ICEV-d", "ICEV-p", "ICEV-g"])

        self.A[
            :,
            self.inputs[
                (
                    "market for brake wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "brake wear emissions, passenger car",
                )
            ],
            ind_A,
        ] = (array[self.array_inputs["driving mass"], :, index].T * 5e-09)

        ind_A = [
            self.inputs[i]
            for i in self.inputs
            if "transport, passenger" in i[0]
            and any(
                x in i[0] for x in ["BEV", "FCEV", "HEV-p", "HEV-d", "PHEV-p", "PHEV-d"]
            )
        ]

        index = self.get_index_vehicle_from_array(
            ["BEV", "FCEV", "HEV-p", "HEV-d", "PHEV-p", "PHEV-d"]
        )

        self.A[
            :,
            self.inputs[
                (
                    "market for brake wear emissions, passenger car",
                    "GLO",
                    "kilogram",
                    "brake wear emissions, passenger car",
                )
            ],
            ind_A,
        ] = (array[self.array_inputs["driving mass"], :, index].T * 5e-09 * 0.2)

        # Infrastructure
        self.A[
            :,
            self.inputs[("market for road", "GLO", "meter-year", "road")],
            [
                self.inputs[i]
                for i in self.inputs
                if "transport, passenger car" in i[0]
            ]
        ] = (5.37e-7 * array[self.array_inputs["driving mass"], :]
             * -1)

        # Infrastructure maintenance
        self.A[
            :,
            self.inputs[
                ("market for road maintenance", "RER", "meter-year", "road maintenance")
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "transport, passenger car" in i[0]
            ]
        ] = (1.29e-3
             * -1)

        # Exhaust emissions
        # Non-fuel based emissions

        self.A[
            np.ix_(
                np.arange(self.iterations),
                self.index_emissions,
                [
                    self.inputs[i]
                    for i in self.inputs
                    if "transport, passenger car" in i[0]
                ]
            )
        ] = (
            array[
                [
                    self.array_inputs[self.map_non_fuel_emissions[self.rev_inputs[x]]]
                    for x in self.index_emissions
                ]
            ]
            * -1
        ).transpose([1, 0, 2])

        # Noise emissions
        self.A[
            np.ix_(
                np.arange(self.iterations),
                self.index_noise,
                [
                    self.inputs[i]
                    for i in self.inputs
                    if "transport, passenger car" in i[0]
                ]
            )
        ] = (
            array[
                [
                    self.array_inputs[self.map_noise_emissions[self.rev_inputs[x]]]
                    for x in self.index_noise
                ]
            ]
            * -1
        ).transpose([1, 0, 2])

        # Emissions of air conditioner refrigerant r134a
        # Leakage assumed to amount to 53g according to
        # https://ec.europa.eu/clima/sites/clima/files/eccp/docs/leakage_rates_final_report_en.pdf

        self.A[
            :,
            self.inputs[
                (
                    "Ethane, 1,1,1,2-tetrafluoro-, HFC-134a",
                    ("air",),
                    "kilogram"
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "transport, passenger car" in i[0]
            ]
        ] = .053 / self.array.values[self.array_inputs["kilometers per year"]] * -1

        self.A[
            :,
            self.inputs[
                (
                    "market for refrigerant R134a",
                    "GLO",
                    "kilogram",
                    "refrigerant R134a"
                )
            ],
            [
                self.inputs[i]
                for i in self.inputs
                if "transport, passenger car" in i[0]
            ]
        ] = .053 / self.array.values[self.array_inputs["kilometers per year"]] * -1

        print("*********************************************************************")
