from __future__ import annotations
from pathlib import Path
import numpy as np
import logging
from datetime import datetime, timedelta
import io
import xarray
import typing as T

try:
    from pymap3d import ecef2geodetic
except ImportError:
    ecef2geodetic = None
#
from .rio import opener, rinexinfo
from .common import determine_time_system, check_time_interval, check_unique_times

"""https://github.com/mvglasow/satstat/wiki/NMEA-IDs"""

SBAS = 100  # offset for ID
GLONASS = 37
QZSS = 192
BEIDOU = 0


def rinexobs3(
    fn: T.TextIO | Path,
    use: set[str] = None,
    tlim: tuple[datetime, datetime] = None,
    useindicators: bool = False,
    meas: list[str] = None,
    verbose: bool = False,
    *,
    fast: bool = False,
    interval: float | int | timedelta = None,
) -> xarray.Dataset:
    """
    process RINEX 3 OBS data

    fn: RINEX OBS 3 filename
    use: 'G'  or ['G', 'R'] or similar

    tlim: read between these time bounds
    useindicators: SSI, LLI are output
    meas:  'L1C'  or  ['L1C', 'C1C'] or similar

    fast:
          TODO: FUTURE, not yet enabled for OBS3
          speculative preallocation based on minimum SV assumption and file size.
          Avoids double-reading file and more complicated linked lists.
          Believed that Numpy array should be faster than lists anyway.
          Reduce Nsvmin if error (let us know)

    interval: allows decimating file read by time e.g. every 5 seconds.
                Useful to speed up reading of very large RINEX files
    """

    interval = check_time_interval(interval)

    if isinstance(use, str):
        use = {use}

    if isinstance(meas, str):
        meas = [meas]

    if not meas or not meas[0].strip():
        meas = None
    # %% allocate
    # times = obstime3(fn)
    data = xarray.Dataset({}, coords={"time": [], "sv": []})
    if tlim is not None and not isinstance(tlim[0], datetime):
        raise TypeError("time bounds are specified as datetime.datetime")

    last_epoch = None
    # %% loop
    with opener(fn) as f:
        hdr = obsheader3(f, use, meas)

        # %% process OBS file
        time_offset = []
        for ln in f:
            if not ln.startswith(">"):  # end of file
                break

            try:
                time = _timeobs(ln)
            except ValueError:  # garbage between header and RINEX data
                logging.debug(f"garbage detected in {fn}, trying to parse at next time step")
                continue

            try:
                time_offset.append(float(ln[41:56]))
            except ValueError:
                pass
            # %% get SV indices
            sv = []
            raw = ""
            # Number of visible satellites this time %i3  pg. A13
            for _ in range(int(ln[33:35])):
                ln = f.readline()
                sv.append(ln[:3])
                raw += ln[3:]

            if tlim is not None:
                if time < tlim[0]:
                    continue
                elif time > tlim[1]:
                    break

            if interval is not None:
                if last_epoch is None:  # initialization
                    last_epoch = time
                else:
                    if time - last_epoch < interval:
                        continue
                    else:
                        last_epoch += interval

            if verbose:
                print(time, end="\r")

            # this time epoch is complete, assemble the data.
            data = _epoch(data, raw, hdr, time, sv, useindicators, verbose)

    # %% patch SV names in case of "G 7" => "G07"
    data = data.assign_coords(sv=[s.replace(" ", "0") for s in data.sv.values.tolist()])
    # %% other attributes
    data.attrs["version"] = hdr["version"]

    # Get interval from header or derive it from the data
    if "interval" in hdr.keys():
        data.attrs["interval"] = hdr["interval"]
    elif "time" in data.coords.keys():
        # median is robust against gaps
        try:
            data.attrs["interval"] = np.median(np.diff(data.time) / np.timedelta64(1, "s"))
        except TypeError:
            pass
    else:
        data.attrs["interval"] = np.nan

    data.attrs["rinextype"] = "obs"
    data.attrs["fast_processing"] = 0  # bool is not allowed in NetCDF4
    data.attrs["time_system"] = determine_time_system(hdr)
    if isinstance(fn, Path):
        data.attrs["filename"] = fn.name

    if "position" in hdr.keys():
        data.attrs["position"] = hdr["position"]
        if ecef2geodetic is not None:
            data.attrs["position_geodetic"] = hdr["position_geodetic"]

    if time_offset:
        data.attrs["time_offset"] = time_offset

    if "RCV CLOCK OFFS APPL" in hdr.keys():
        try:
            data.attrs["receiver_clock_offset_applied"] = int(hdr["RCV CLOCK OFFS APPL"])
        except ValueError:
            pass

    return data


def _timeobs(ln: str) -> datetime:
    """
    convert time from RINEX 3 OBS text to datetime
    """

    if not ln.startswith("> "):  # pg. A13
        raise ValueError('RINEX 3 line beginning "> " is not present')

    return datetime(
        int(ln[2:6]),
        int(ln[7:9]),
        int(ln[10:12]),
        hour=int(ln[13:15]),
        minute=int(ln[16:18]),
        second=int(ln[19:21]),
        microsecond=int(float(ln[19:29]) % 1 * 1000000),
    )


def obstime3(fn: T.TextIO | Path, verbose: bool = False) -> np.ndarray:
    """
    return all times in RINEX file
    """

    times = []

    with opener(fn) as f:
        for ln in f:
            if ln.startswith("> "):
                try:
                    times.append(_timeobs(ln))
                except (ValueError, IndexError):
                    logging.debug(f"was not a time:\n{ln}")
                    continue

    times = np.asarray(times)

    check_unique_times(times)

    return times


def _epoch(
    data: xarray.Dataset,
    raw: str,
    hdr: dict[str, T.Any],
    time: datetime,
    sv: list[str],
    useindicators: bool,
    verbose: bool,
) -> xarray.Dataset:
    """
    block processing of each epoch (time step)
    """
    darr = np.atleast_2d(
        np.genfromtxt(io.BytesIO(raw.encode("ascii")), delimiter=(14, 1, 1) * hdr["Fmax"])
    )
    # %% assign data for each time step
    for sk in hdr["fields"]:  # for each satellite system type (G,R,S, etc.)
        # satellite indices "si" to extract from this time's measurements
        si = [i for i, s in enumerate(sv) if s[0] in sk]
        if len(si) == 0:  # no SV of this system "sk" at this time
            continue

        # measurement indices "di" to extract at this time step
        di = hdr["fields_ind"][sk]
        garr = darr[si, :]
        garr = garr[:, di]

        gsv = np.array(sv)[si]

        dsf: dict[str, tuple] = {}
        for i, k in enumerate(hdr["fields"][sk]):
            dsf[k] = (("time", "sv"), np.atleast_2d(garr[:, i * 3]))

            if useindicators:
                dsf = _indicators(dsf, k, garr[:, i * 3 + 1 : i * 3 + 3])

        if verbose:
            print(time, "\r", end="")

        epoch_data = xarray.Dataset(dsf, coords={"time": [time], "sv": gsv})
        if len(data) == 0:
            data = epoch_data
        elif len(hdr["fields"]) == 1:  # one satellite system selected, faster to process
            data = xarray.concat((data, epoch_data), dim="time")
        else:  # general case, slower for different satellite systems all together
            data = xarray.merge((data, epoch_data))

    return data


def _indicators(d: dict, k: str, arr: np.ndarray) -> dict[str, tuple]:
    """
    handle LLI (loss of lock) and SSI (signal strength)
    """
    if k.startswith(("L1", "L2")):
        d[k + "lli"] = (("time", "sv"), np.atleast_2d(arr[:, 0]))

    d[k + "ssi"] = (("time", "sv"), np.atleast_2d(arr[:, 1]))

    return d


def obsheader3(f: T.TextIO, use: set[str] = None, meas: list[str] = None) -> dict[str, T.Any]:
    """
    get RINEX 3 OBS types, for each system type
    optionally, select system type and/or measurement type to greatly
    speed reading and save memory (RAM, disk)
    """
    if isinstance(f, (str, Path)):
        with opener(f, header=True) as h:
            return obsheader3(h, use, meas)

    fields = {}
    Fmax = 0

    # %% first line
    hdr = rinexinfo(f)

    for ln in f:
        if "END OF HEADER" in ln:
            break

        hd = ln[60:80]
        c = ln[:60]
        if "SYS / # / OBS TYPES" in hd:
            k = c[0]
            fields[k] = c[6:60].split()
            N = int(c[3:6])
            # %% maximum number of fields in a file, to allow fast Numpy parse.
            Fmax = max(N, Fmax)

            n = N - 13
            while n > 0:  # Rinex 3.03, pg. A6, A7
                ln = f.readline()
                assert "SYS / # / OBS TYPES" in ln[60:]
                fields[k] += ln[6:60].split()
                n -= 13

            assert len(fields[k]) == N

            continue

        if hd.strip() not in hdr:  # Header label
            hdr[hd.strip()] = c  # don't strip for fixed-width parsers
            # string with info
        else:  # concatenate to the existing string
            hdr[hd.strip()] += " " + c

    # %% list with x,y,z cartesian (OPTIONAL)
    # Rinex 3.03, pg. A6, Table A2
    try:
        # some RINEX files have bad headers with mulitple APPROX POSITION XYZ.
        # we choose to use the first such header.
        hdr["position"] = [float(j) for j in hdr["APPROX POSITION XYZ"].split()][:3]
        if ecef2geodetic is not None and len(hdr["position"]) == 3:
            hdr["position_geodetic"] = ecef2geodetic(*hdr["position"])
    except (KeyError, ValueError):
        pass
    # %% time
    try:
        t0s = hdr["TIME OF FIRST OBS"]
        # NOTE: must do second=int(float()) due to non-conforming files
        hdr["t0"] = datetime(
            year=int(t0s[:6]),
            month=int(t0s[6:12]),
            day=int(t0s[12:18]),
            hour=int(t0s[18:24]),
            minute=int(t0s[24:30]),
            second=int(float(t0s[30:36])),
            microsecond=int(float(t0s[30:43]) % 1 * 1000000),
        )
    except (KeyError, ValueError):
        pass

    try:
        hdr["interval"] = float(hdr["INTERVAL"][:10])
    except (KeyError, ValueError):
        pass
    # %% select specific satellite systems only (optional)
    if use:
        if not set(fields.keys()).intersection(use):
            raise KeyError(f"system type {use} not found in RINEX file")

        fields = {k: fields[k] for k in use if k in fields}

    # perhaps this could be done more efficiently, but it's probably low impact on overall program.
    # simple set and frozenset operations do NOT preserve order, which would completely mess up reading!
    sysind: dict[str, T.Any] = {}
    if isinstance(meas, (tuple, list, np.ndarray)):
        for sk in fields:  # iterate over each system
            # ind = np.isin(fields[sk], meas)  # boolean vector
            ind = np.zeros(len(fields[sk]), dtype=bool)
            for m in meas:
                for i, field in enumerate(fields[sk]):
                    if field.startswith(m):
                        ind[i] = True

            fields[sk] = np.array(fields[sk])[ind].tolist()
            sysind[sk] = np.empty(Fmax * 3, dtype=bool)  # *3 due to LLI, SSI
            for j, i in enumerate(ind):
                sysind[sk][j * 3 : j * 3 + 3] = i
    else:
        sysind = {k: np.s_[:] for k in fields}

    hdr["fields"] = fields
    hdr["fields_ind"] = sysind
    hdr["Fmax"] = Fmax

    return hdr
