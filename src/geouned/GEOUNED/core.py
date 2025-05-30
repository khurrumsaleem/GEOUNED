import json
import logging
import typing
from datetime import datetime
from pathlib import Path
from typing import get_type_hints
from importlib.metadata import version

import FreeCAD
import Part
from tqdm import tqdm

from .code_version import *
from .utils.log_utils import setup_logger
from .conversion import cell_definition as Conv
from .cuboid.translate import translate
from .decompose import decom_one as Decom
from .loadfile import load_step as Load
from .utils import functions as UF
from .utils.boolean_solids import build_c_table_from_solids
from .utils.data_classes import NumericFormat, Options, Settings, Tolerances
from .void import void as void
from .write.functions import write_mcnp_cell_def
from .write.write_files import write_geometry

logger = logging.getLogger("general_logger")


class CadToCsg:
    """Base class for the conversion of CAD to CSG models

    Args:
        options (geouned.Options, optional): An instance of a geouned.Options
            class with the attributes set for the desired conversion. Defaults
            to a geouned.Options with default attributes values.
        tolerances (geouned.Tolerances, optional): An instance of a
            geouned.Tolerances class with the attributes set for the desired
            conversion. Defaults to a geouned.Tolerances with default
            attributes values.
        numeric_format (geouned.NumericFormat, optional): An instance of a
            geouned.NumericFormat class with the attributes set for the desired
            conversion. Defaults to a geouned.NumericFormat with default
            attributes values.
        settings (geouned.Settings, optional): An instance of a
            geouned.Settings class with the attributes set for the desired
            conversion. Defaults to a geouned.Settings with default
            attributes values.
    """

    def __init__(
        self,
        options: Options = Options(),
        tolerances: Tolerances = Tolerances(),
        numeric_format: NumericFormat = NumericFormat(),
        settings: Settings = Settings(),
    ):

        self.options = options
        self.tolerances = tolerances
        self.numeric_format = numeric_format
        self.settings = settings

        # define later when running the code
        self.geometry_bounding_box = None
        self.meta_list = []
        self.filename = None
        self.skip_solids = []

        log_path = Path(self.settings.outPath) / "log_files"
        log_path.mkdir(parents=True, exist_ok=True)
        setup_logger("general_logger", log_path / "geouned_general.log")
        setup_logger("fuzzy_logger", log_path / "geouned_fuzzy.log")
        setup_logger("solids_logger", log_path / "geouned_solids.log")
        logger.info(f"GEOUNED version {version('geouned')}")
        logger.info(f"FreeCAD version {'.'.join(FreeCAD.Version()[:3])}")

    @property
    def options(self):
        return self._options

    @options.setter
    def options(self, value: Options):
        if not isinstance(value, Options):
            raise TypeError(f"geouned.CadToCsg.options should be an instance of geouned.Options, not a {type(value)}")
        self._options = value

    @property
    def tolerances(self):
        return self._tolerances

    @tolerances.setter
    def tolerances(self, value: tolerances):
        if not isinstance(value, Tolerances):
            raise TypeError(f"geouned.CadToCsg.tolerances should be an instance of geouned.Tolerances, not a {type(value)}")
        self._tolerances = value

    @property
    def numeric_format(self):
        return self._numeric_format

    @numeric_format.setter
    def numeric_format(self, value: numeric_format):
        if not isinstance(value, NumericFormat):
            raise TypeError(
                f"geouned.CadToCsg.numeric_format should be an instance of geouned.NumericFormat, not a {type(value)}"
            )
        self._numeric_format = value

    @property
    def settings(self):
        return self._settings

    @settings.setter
    def settings(self, value: settings):
        if not isinstance(value, Settings):
            raise TypeError(f"geouned.CadToCsg.settings should be an instance of geouned.Settings, not a {type(value)}")
        self._settings = value

    def export_csg(
        self,
        title: str = "Converted with GEOUNED",
        geometryName: str = "csg",
        outFormat: typing.Tuple[str] = (
            "openmc_xml",
            "openmc_py",
            "serpent",
            "phits",
            "mcnp",
        ),
        volSDEF: bool = False,
        volCARD: bool = True,
        UCARD: typing.Union[int, None] = None,
        dummyMat: bool = False,
        cellCommentFile: bool = False,
        cellSummaryFile: bool = True,
    ):
        """Writes out a CSG file in the requested Monte Carlo code format.

        Args:
            title (str, optional): Title of the model written at the top of the
                output file. Defaults to "Geouned conversion".
            geometryName (str, optional): the file stem of the output file(s).
                Defaults to "converted_with_geouned".
            outFormat (typing.Tuple[str], optional): Format for the output
                geometry. Available format are: "mcnp", "openmc_xml",
                "openmc_py", "phits" and "serpent". Several output format can
                be written in the same method call. Defaults to output all codes.
            volSDEF (bool, optional):  Write SDEF definition and tally of solid
                cell for stochastic volume checking. Defaults to False.
            volCARD (bool, optional): Write the CAD calculated volume in the
                cell definition using the VOL card. Defaults to True.
            UCARD (int, optional): Write universe card in the cell definition
                with the specified universe number (if value = 0 Universe card
                is not written). Defaults to None.
            dummyMat (bool, optional): Write dummy material definition card in
               the MCNP output file for all material labels present in the
               model. Dummy material definition is "MX 1001 1". Defaults to False.
            cellCommentFile (bool, optional): Write an additional file with
               comment associated to each CAD cell in the MCNP output file.
               Defaults to False.
            cellSummaryFile (bool, optional): Write an additional file with
               information on the CAD cell translated. Defaults to True.
        """

        if not isinstance(UCARD, int) and not isinstance(UCARD, type(None)):
            raise TypeError(f"UCARD should be of type int or None not {type(UCARD)}")
        if isinstance(UCARD, int):
            if UCARD < 0:
                raise ValueError("UCARD should be a 0 or a positive integer ")

        for arg, arg_str in (
            (volSDEF, "volSDEF"),
            (volCARD, "volCARD"),
            (dummyMat, "dummyMat"),
            (cellCommentFile, "cellCommentFile"),
            (cellSummaryFile, "cellSummaryFile"),
        ):
            if not isinstance(arg, bool):
                raise TypeError(f"{arg} should be of type bool not {type(arg_str)}")

        for arg, arg_str in ((title, "title"), (geometryName, "geometryName")):
            if not isinstance(arg, str):
                raise TypeError(f"{arg} should be of type str not {type(arg_str)}")

        # if the geometry_bounding_box has not previuosly been calculated, then make a default one
        if self.geometry_bounding_box is None:
            self._get_geometry_bounding_box()

        write_geometry(
            UniverseBox=self.geometry_bounding_box,
            MetaList=self.meta_list,
            Surfaces=self.Surfaces,
            settings=self.settings,
            options=self.options,
            tolerances=self.tolerances,
            numeric_format=self.numeric_format,
            geometryName=geometryName,
            outFormat=outFormat,
            cellCommentFile=cellCommentFile,
            cellSummaryFile=cellSummaryFile,
            title=title,
            volSDEF=volSDEF,
            volCARD=volCARD,
            UCARD=UCARD,
            dummyMat=dummyMat,
            step_filename=self.filename,
        )

        logger.info("End of Monte Carlo code translation phase")

    @classmethod
    def from_json(cls, filename: str):
        """Creates a CadToCsg instance, runs CadToCsg.load_Step_file(), runs
        CadToCsg.start() and returns the instance. Populating the arguments for
        the methods that are run by looking for keys with the same name as the
        method in the JSON file. For example CadToCsg.start() accepts arguments
        for Options, Tolerance, Settings and NumericFormat and can be populated
        from matching key names in the JSON. If export_to_csg key is present
        then this method also runs CadToCsg.export_to_csg() on the instance.

        Args:
            filename str: The filename of the config file.

        Raises:
            FileNotFoundError: If the config file is not found
            ValueError: If the config JSON file is found to contain an invalid key

        Returns:
            geouned.CadToCsg: returns a geouned CadToCsg class instance.
        """

        if not Path(filename).exists():
            raise FileNotFoundError(f"config file {filename} not found")

        with open(filename) as f:
            config = json.load(f)

        cad_to_csg = cls()

        for key in config.keys():

            if key in ["load_step_file", "export_csg"]:
                pass  # these two keys are used before or after this loop

            elif key == "Tolerances":
                cad_to_csg.tolerances = Tolerances(**config["Tolerances"])

            elif key == "Options":
                cad_to_csg.options = Options(**config["Options"])

            elif key == "NumericFormat":
                cad_to_csg.numeric_format = NumericFormat(**config["NumericFormat"])

            elif key == "Settings":
                cad_to_csg.settings = Settings(**config["Settings"])

            else:
                raise ValueError(
                    f"Invalid key '{key}' found in config file {filename}. Acceptable key names are 'load_step_file', 'export_csg', 'Settings', 'Parameters', 'Tolerances' and 'NumericFormat'"
                )

        cad_to_csg.load_step_file(**config["load_step_file"])
        cad_to_csg.start()
        if "export_csg" in config.keys():
            cad_to_csg.export_csg(**config["export_csg"])
        else:
            cad_to_csg.export_csg()
        return cad_to_csg

    def load_step_file(
        self,
        filename: typing.Union[str, typing.Sequence[str]],
        skip_solids: typing.Sequence[int] = [],
        spline_surfaces: str = "stop",
    ):
        """
        Load STEP file(s) and extract solid volumes and enclosure volumes.

        Args:
            filename (str): The path to the STEP file or a list of paths to multiple STEP files.
            skip_solids (Sequence[int], optional): A sequence (list or tuple) of indexes of solids to not load for conversion.
            spline_surfaces (str): Behavior of the code if solids with spline surface are considered: 'stop' execution, 'remove' solid,
                                   'ignore' solid is included for translation (may lead to translation errors)

        Returns:
            tuple: A tuple containing the solid volumes list and enclosure volumes list extracted from the STEP files.
        """
        logger.info("Start of step file loading phase")

        if not isinstance(skip_solids, (list, tuple)):
            raise TypeError(f"skip_solids should be a list, tuple of ints, not a {type(skip_solids)}")
        for entry in skip_solids:
            if not isinstance(entry, int):
                raise TypeError(f"skip_solids should contain only ints, not a {type(entry)}")

        if not isinstance(filename, (str, list, tuple)):
            raise TypeError(f"filename should be a str or a sequence of str, not a {type(filename)}")
        if isinstance(filename, (list, tuple)):
            for entry in filename:
                if not isinstance(entry, str):
                    raise TypeError(f"filename should contain only str, not a {type(entry)}")

        if not isinstance(spline_surfaces, str):
            raise TypeError(f"filename should be a str, not a {type(filename)}")
        if spline_surfaces.lower() not in ("stop", "remove", "ignore"):
            raise TypeError(f'available values for spline_surfaces are: "stop", "remove" or "ignore" ')

        self.filename = filename
        self.skip_solids = skip_solids

        if isinstance(filename, (list, tuple)):
            step_files = filename
        else:
            step_files = [filename]

        for step_file in step_files:
            if not Path(step_file).is_file():
                raise FileNotFoundError(f"Step file {step_file} not found.")

        MetaChunk = []
        EnclosureChunk = []
        for step_file in tqdm(step_files, desc="Loading CAD files"):
            logger.info(f"read step file : {step_file}")
            Meta, Enclosure = Load.load_cad(step_file, spline_surfaces, self.settings, self.options)
            MetaChunk.append(Meta)
            EnclosureChunk.append(Enclosure)
        self.meta_list = join_meta_lists(MetaChunk)
        self.enclosure_list = join_meta_lists(EnclosureChunk)

        # deleting the solid index in reverse order so the indexes don't change for subsequent deletions
        for solid_index in sorted(skip_solids, reverse=True):
            logger.info(f"Removing solid index: {solid_index} from list of {len(self.meta_list)} solids")
            del self.meta_list[solid_index]

        for m in reversed(self.enclosure_list):
            if m.Solids is None:
                print("stop because spline surfaces found in enclosure solid")
                exit()

        for m in reversed(self.meta_list):
            if m.Solids is None:
                self.meta_list.remove(m)

        logger.info("End of step file loading phase")

        return self.meta_list, self.enclosure_list

    def _export_solids(self, filename: str):
        """Export all the solid volumes from the loaded geometry to a STEP file.

        Args:
            filename (str): filepath of the output STEP file.
        """
        # export in STEP format solids read from input file
        if self.meta_list == []:
            raise ValueError(
                "No solids in CadToCsg.meta_list to export. Try loading the STEP file first with CadToCsg.load_step_file"
            )
        solids = []
        for m in self.meta_list:
            if m.IsEnclosure:
                continue
            solids.extend(m.Solids)
        Part.makeCompound(solids).exportStep(filename)

    def _get_geometry_bounding_box(self, padding: float = 10.0):
        """
        Get the bounding box of the geometry.

        Args:
            padding (float): The padding value to add to the bounding box dimensions.

        Returns:
            FreeCAD.BoundBox: The universe bounding box.
        """
        # set up Universe
        meta_list = self.meta_list

        Box = meta_list[0].optimalBoundingBox()
        xmin = Box.XMin
        xmax = Box.XMax
        ymin = Box.YMin
        ymax = Box.YMax
        zmin = Box.ZMin
        zmax = Box.ZMax
        for m in meta_list[1:]:
            # MIO. This was removed since in HELIAS the enclosure cell is the biggest one
            # if m.IsEnclosure: continue
            optBox = m.optimalBoundingBox()
            xmin = min(optBox.XMin, xmin)
            xmax = max(optBox.XMax, xmax)
            ymin = min(optBox.YMin, ymin)
            ymax = max(optBox.YMax, ymax)
            zmin = min(optBox.ZMin, zmin)
            zmax = max(optBox.ZMax, zmax)

        self.geometry_bounding_box = FreeCAD.BoundBox(
            FreeCAD.Vector(xmin - padding, ymin - padding, zmin - padding),
            FreeCAD.Vector(xmax + padding, ymax + padding, zmax + padding),
        )
        return self.geometry_bounding_box

    def start(self):

        if len(self.meta_list) == 0:
            print("no solid selected to translate")
            exit()

        startTime = datetime.now()

        if self.settings.exportSolids:
            self._export_solids(filename=self.settings.exportSolids)

        logger.info("End of loading phase")
        tempstr1 = str(datetime.now() - startTime)
        logger.info(tempstr1)
        tempTime = datetime.now()

        # sets self.geometry_bounding_box with default padding
        self._get_geometry_bounding_box()

        self.Surfaces = UF.SurfacesDict(offset=self.settings.startSurf - 1)

        warnSolids = []
        warnEnclosures = []
        coneInfo = dict()
        tempTime0 = datetime.now()
        if not self.options.Facets:

            # decompose all solids in elementary solids (convex ones)
            warningSolidList = self._decompose_solids(meta=True)

            # decompose Enclosure solids
            if self.settings.voidGen and self.enclosure_list:
                warningEnclosureList = self._decompose_solids(meta=False)

            logger.info("End of decomposition phase")

            # start Building CGS cells phase

            for j, m in enumerate(tqdm(self.meta_list, desc="Translating solid cells")):
                if m.IsEnclosure:
                    continue
                logger.info(f"Building cell: {j+1}")
                cones = Conv.cellDef(
                    m,
                    self.Surfaces,
                    self.geometry_bounding_box,
                    self.options,
                    self.tolerances,
                    self.numeric_format,
                )
                if cones:
                    coneInfo[m.__id__] = cones
                if j in warningSolidList:
                    warnSolids.append(m)
                if not m.Solids:
                    logger.info(f"none {j}, {m.__id__}")
                    logger.info(m.Definition)

            if self.options.forceNoOverlap:
                Conv.no_overlapping_cell(self.meta_list, self.Surfaces, self.options)

        else:
            translate(
                self.meta_list,
                self.Surfaces,
                self.geometry_bounding_box,
                self.settings,
                self.options,
                self.tolerances,
            )
            # decompose Enclosure solids
            if self.settings.voidGen and self.enclosure_list:
                warningEnclosureList = self._decompose_solids(meta=False)

        tempstr2 = str(datetime.now() - tempTime)
        logger.info(tempstr2)

        #  building enclosure solids

        if self.settings.voidGen and self.enclosure_list:
            for j, m in enumerate(self.enclosure_list):
                logger.info(f"Building Enclosure Cell: {j + 1}")
                cones = Conv.cellDef(
                    m,
                    self.Surfaces,
                    self.geometry_bounding_box,
                    self.options,
                    self.tolerances,
                    self.numeric_format,
                )
                if cones:
                    coneInfo[m.__id__] = cones
                if j in warningEnclosureList:
                    warnEnclosures.append(m)

        tempTime1 = datetime.now()

        # void generation phase
        meta_void = []
        if self.settings.voidGen:
            logger.info("Build Void")
            logger.info(self.settings.voidExclude)
            if not self.settings.voidExclude:
                meta_reduced = self.meta_list
            else:
                meta_reduced = exclude_cells(self.meta_list, self.settings.voidExclude)

            if self.meta_list:
                init = self.meta_list[-1].__id__ - len(self.enclosure_list)
            else:
                init = 0
            meta_void = void.void_generation(
                meta_reduced,
                self.enclosure_list,
                self.Surfaces,
                self.geometry_bounding_box,
                self.settings,
                init,
                self.options,
                self.tolerances,
                self.numeric_format,
            )

        # if self.settings.simplify == 'full' and not self.options.forceNoOverlap:
        if self.settings.simplify == "full":
            Surfs = {}
            for lst in self.Surfaces.values():
                for s in lst:
                    Surfs[s.Index] = s

            for c in tqdm(self.meta_list, desc="Simplifying"):
                if c.Definition.level == 0 or c.IsEnclosure:
                    continue
                logger.info(f"simplify cell {c.__id__}")
                Box = UF.get_box(c, self.options.enlargeBox)
                CT = build_c_table_from_solids(Box, (c.Surfaces, Surfs), "full", options=self.options)
                c.Definition.simplify(CT)
                c.Definition.clean()
                if type(c.Definition.elements) is bool:
                    logger.info(f"unexpected constant cell {c.__id__} :{c.Definition.elements}")

        tempTime2 = datetime.now()
        logger.info(f"build Time: {tempTime2} - {tempTime1}")

        logger.info(datetime.now() - startTime)

        cellOffSet = self.settings.startCell - 1
        if self.enclosure_list and self.settings.sort_enclosure:
            # sort group solid cell / void cell sequence in each for each enclosure
            # if a solid belong to several enclosure, its definition will be written
            # for the highest enclosure level or if same enclosure level in the first
            # enclosure found
            self.meta_list = sort_enclosure(self.meta_list, meta_void, cellOffSet)
        else:
            # remove Null Cell and apply cell numbering offset
            deleted = []
            idLabel = {0: 0}
            icount = cellOffSet
            for i, m in enumerate(self.meta_list):
                if m.NullCell or m.IsEnclosure:
                    deleted.append(i)
                    continue

                icount += 1
                m.label = icount
                idLabel[m.__id__] = m.label

            for i in reversed(deleted):
                del self.meta_list[i]

            lineComment = """\
##########################################################
             VOID CELLS
##########################################################"""
            mc = UF.GeounedSolid(None)
            mc.Comments = lineComment
            self.meta_list.append(mc)

            deleted = []
            for i, m in enumerate(meta_void):
                if m.NullCell:
                    deleted.append(i)
                    continue
                icount += 1
                m.label = icount
                update_comment(m, idLabel)
            for i in reversed(deleted):
                del meta_void[i]

            self.meta_list.extend(meta_void)

        print_warning_solids(warnSolids, warnEnclosures)

        # add plane definition to cone
        process_cones(
            self.meta_list,
            coneInfo,
            self.Surfaces,
            self.geometry_bounding_box,
            self.options,
            self.tolerances,
            self.numeric_format,
        )

        logger.info("Process finished")
        logger.info(datetime.now() - startTime)

        logger.info(f"Translation time of solid cells {tempTime1} - {tempTime0}")
        logger.info(f"Translation time of void cells {tempTime2} - {tempTime1}")

    def _decompose_solids(self, meta: bool):

        if meta:
            meta_list = self.meta_list
            description = "Decomposing solids"
        else:
            meta_list = self.enclosure_list
            description = "Decomposing enclosure solids"

        totsolid = len(meta_list)
        warningSolids = []
        for i, m in enumerate(tqdm(meta_list, desc=description)):
            if meta and m.IsEnclosure:
                continue
            logger.info(f"Decomposing solid: {i + 1}/{totsolid}")
            if self.settings.debug:
                debug_output_folder = Path(self.settings.outPath) / "debug"
                debug_output_folder.mkdir(parents=True, exist_ok=True)
                logger.info(m.Comments)
                if m.IsEnclosure:
                    m.Solids[0].exportStep(str(debug_output_folder / f"origEnclosure_{i}.stp"))
                else:
                    m.Solids[0].exportStep(str(debug_output_folder / f"origSolid_{i}.stp"))

            comsolid, err = Decom.SplitSolid(
                Part.makeCompound(m.Solids),
                self.geometry_bounding_box,
                self.options,
                self.tolerances,
                self.numeric_format,
            )

            if err != 0:
                sus_output_folder = Path(self.settings.outPath) / "suspicious_solids"
                sus_output_folder.mkdir(parents=True, exist_ok=True)
                if m.IsEnclosure:
                    Part.CompSolid(m.Solids).exportStep(str(sus_output_folder / f"Enclosure_original_{i}.stp"))
                    comsolid.exportStep(str(sus_output_folder / f"Enclosure_split_{i}.stp"))
                else:
                    Part.CompSolid(m.Solids).exportStep(str(sus_output_folder / f"Solid_original_{i}.stp"))
                    comsolid.exportStep(str(sus_output_folder / f"Solid_split_{i}.stp"))

                warningSolids.append(i)

            if self.settings.debug:
                if m.IsEnclosure:
                    comsolid.exportStep(str(debug_output_folder / f"compEnclosure_{i}.stp"))
                else:
                    comsolid.exportStep(str(debug_output_folder / f"compSolid_{i}.stp"))
            self.Surfaces.extend(
                Decom.extract_surfaces(
                    comsolid,
                    "All",
                    self.geometry_bounding_box,
                    self.options,
                    self.tolerances,
                    self.numeric_format,
                    MakeObj=True,
                ),
                self.options,
                self.tolerances,
                self.numeric_format,
            )
            m.set_cad_solid()
            m.update_solids(comsolid.Solids)

        return warningSolids


def update_comment(meta, idLabel):
    if meta.__commentInfo__ is None:
        return
    if meta.__commentInfo__[1] is None:
        return
    newLabel = (idLabel[i] for i in meta.__commentInfo__[1])
    meta.set_comments(void.void_comment_line((meta.__commentInfo__[0], newLabel)))


def process_cones(MetaList, coneInfo, Surfaces, UniverseBox, options, tolerances, numeric_format):
    cellId = tuple(coneInfo.keys())
    for m in MetaList:
        if m.__id__ not in cellId and not m.Void:
            continue

        if m.Void and m.__commentInfo__ is not None:
            if m.__commentInfo__[1] is None:
                continue
            cones = set()
            for Id in m.__commentInfo__[1]:
                if Id in cellId:
                    cones.update(-x for x in coneInfo[Id])
            Conv.add_cone_plane(
                m.Definition,
                cones,
                Surfaces,
                UniverseBox,
                options,
                tolerances,
                numeric_format,
            )
        elif not m.Void:
            Conv.add_cone_plane(
                m.Definition,
                coneInfo[m.__id__],
                Surfaces,
                UniverseBox,
                options,
                tolerances,
                numeric_format,
            )


def print_warning_solids(warnSolids, warnEnclosures):

    solids_logger = logging.getLogger("solids_logger")

    if warnSolids or warnEnclosures:
        pass
    else:
        return

    if warnSolids:
        lines = "Solids :\n"
        for sol in warnSolids:
            lines += "\n"
            lines += f"{sol.label}\n"
            lines += f"{sol.Comments}\n"
            lines += f"{write_mcnp_cell_def(sol.Definition)}\n"
        solids_logger.info(lines)

    if warnEnclosures:
        lines = "Enclosures :\n"
        for sol in warnEnclosures:
            lines += "\n"
            lines += f"{sol.label}\n"
            lines += f"{sol.Comments}\n"
            lines += f"{write_mcnp_cell_def(sol.Definition)}\n"

        solids_logger.info(lines)


def join_meta_lists(MList) -> typing.List[UF.GeounedSolid]:

    newMetaList = MList[0]
    if MList[0]:
        for M in MList[1:]:
            lastID = newMetaList[-1].__id__ + 1
            for i, meta in enumerate(M):
                meta.__id__ = lastID + i
                newMetaList.append(meta)
    return newMetaList


def exclude_cells(MetaList, labelList):
    voidMeta = []
    for m in MetaList:
        if m.IsEnclosure:
            continue
        found = False
        for label in labelList:
            if label in m.Comments:
                found = True
                break
        if not found:
            voidMeta.append(m)

    return voidMeta


def sort_enclosure(MetaList, meta_void, offSet=0):

    newList = {}
    for m in meta_void:
        if m.EnclosureID in newList.keys():
            newList[m.EnclosureID].append(m)
        else:
            newList[m.EnclosureID] = [m]

    icount = offSet
    idLabel = {0: 0}
    newMeta = []
    for m in MetaList:
        if m.NullCell:
            continue
        if m.IsEnclosure:
            lineComment = f"""##########################################################
             ENCLOSURE {m.EnclosureID}
##########################################################"""
            mc = UF.GeounedSolid(None)
            mc.Comments = lineComment
            newMeta.append(mc)
            for e in newList[m.EnclosureID]:
                if e.NullCell:
                    continue
                icount += 1
                e.label = icount
                idLabel[e.__id__] = e.label
                newMeta.append(e)
            lineComment = f"""##########################################################
            END  ENCLOSURE {m.EnclosureID}
##########################################################"""
            mc = UF.GeounedSolid(None)
            mc.Comments = lineComment
            newMeta.append(mc)

        else:
            icount += 1
            m.label = icount
            idLabel[m.__id__] = m.label
            newMeta.append(m)

    lineComment = """\
##########################################################
             VOID CELLS 
##########################################################"""
    mc = UF.GeounedSolid(None)
    mc.Comments = lineComment
    newMeta.append(mc)

    for v in newList[0]:
        if v.NullCell:
            continue
        icount += 1
        v.label = icount
        idLabel[v.__id__] = v.label
        newMeta.append(v)

    for m in newMeta:
        if not m.Void:
            continue
        if m.IsEnclosure:
            continue
        update_comment(m, idLabel)

    return newMeta
