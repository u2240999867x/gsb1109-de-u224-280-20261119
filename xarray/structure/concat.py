from __future__ import annotations

import functools
from collections.abc import Hashable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Union, overload

import numpy as np
import pandas as pd

from xarray.core import dtypes, utils
from xarray.core.coordinates import Coordinates
from xarray.core.duck_array_ops import lazy_array_equiv
from xarray.core.indexes import Index, PandasIndex
from xarray.core.types import T_DataArray, T_Dataset, T_Variable
from xarray.core.utils import emit_user_level_warning, is_dict_like
from xarray.core.variable import Variable, _as_shared_dtype
from xarray.core.variable import concat as concat_vars
from xarray.namedarray.pycompat import is_chunked_array
from xarray.structure.alignment import align, reindex_variables
from xarray.structure.merge import (
    _VALID_COMPAT,
    collect_variables_and_indexes,
    merge_attrs,
    merge_collected,
)
from xarray.util.deprecation_helpers import (
    _COMPAT_CONCAT_DEFAULT,
    _COORDS_DEFAULT,
    _DATA_VARS_DEFAULT,
    _JOIN_DEFAULT,
    CombineKwargDefault,
)

if TYPE_CHECKING:
    from xarray.core.datatree import DataTree
    from xarray.core.types import (
        CombineAttrsOptions,
        CompatOptions,
        ConcatOptions,
        JoinOptions,
    )

    T_DataVars = Union[ConcatOptions, Iterable[Hashable], None]


@overload
def concat(
    objs: Iterable[DataTree],
    dim: Hashable | T_Variable | T_DataArray | pd.Index | Any,
    data_vars: T_DataVars | CombineKwargDefault = _DATA_VARS_DEFAULT,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault = _COORDS_DEFAULT,
    compat: CompatOptions | CombineKwargDefault = _COMPAT_CONCAT_DEFAULT,
    positions: Iterable[Iterable[int]] | None = None,
    fill_value: object = dtypes.NA,
    join: JoinOptions | CombineKwargDefault = _JOIN_DEFAULT,
    combine_attrs: CombineAttrsOptions = "override",
    create_index_for_new_dim: bool = True,
) -> DataTree: ...


# TODO: replace dim: Any by 1D array_likes
@overload
def concat(
    objs: Iterable[T_Dataset],
    dim: Hashable | T_Variable | T_DataArray | pd.Index | Any,
    data_vars: T_DataVars | CombineKwargDefault = _DATA_VARS_DEFAULT,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault = _COORDS_DEFAULT,
    compat: CompatOptions | CombineKwargDefault = _COMPAT_CONCAT_DEFAULT,
    positions: Iterable[Iterable[int]] | None = None,
    fill_value: object = dtypes.NA,
    join: JoinOptions | CombineKwargDefault = _JOIN_DEFAULT,
    combine_attrs: CombineAttrsOptions = "override",
    create_index_for_new_dim: bool = True,
) -> T_Dataset: ...


@overload
def concat(
    objs: Iterable[T_DataArray],
    dim: Hashable | T_Variable | T_DataArray | pd.Index | Any,
    data_vars: T_DataVars | CombineKwargDefault = _DATA_VARS_DEFAULT,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault = _COORDS_DEFAULT,
    compat: CompatOptions | CombineKwargDefault = _COMPAT_CONCAT_DEFAULT,
    positions: Iterable[Iterable[int]] | None = None,
    fill_value: object = dtypes.NA,
    join: JoinOptions | CombineKwargDefault = _JOIN_DEFAULT,
    combine_attrs: CombineAttrsOptions = "override",
    create_index_for_new_dim: bool = True,
) -> T_DataArray: ...


def concat(
    objs,
    dim,
    data_vars: T_DataVars | CombineKwargDefault = _DATA_VARS_DEFAULT,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault = _COORDS_DEFAULT,
    compat: CompatOptions | CombineKwargDefault = _COMPAT_CONCAT_DEFAULT,
    positions=None,
    fill_value=dtypes.NA,
    join: JoinOptions | CombineKwargDefault = _JOIN_DEFAULT,
    combine_attrs: CombineAttrsOptions = "override",
    create_index_for_new_dim: bool = True,
):
    """Concatenate xarray objects along a new or existing dimension.

    Parameters
    ----------
    objs : sequence of DataArray, Dataset or DataTree
        xarray objects to concatenate together. Each object is expected to
        consist of variables and coordinates with matching shapes except for
        along the concatenated dimension.
    dim : Hashable or Variable or DataArray or pandas.Index
        Name of the dimension to concatenate along. This can either be a new
        dimension name, in which case it is added along axis=0, or an existing
        dimension name, in which case the location of the dimension is
        unchanged. If dimension is provided as a Variable, DataArray or Index, its name
        is used as the dimension to concatenate along and the values are added
        as a coordinate.
    data_vars : {"minimal", "different", "all", None} or list of str, default: "all"
        These data variables will be concatenated together:
          * "minimal": Only data variables in which the dimension already
            appears are included.
          * "different": Data variables which are not equal (ignoring
            attributes) across all datasets are also concatenated (as well as
            all for which dimension already appears). Beware: this option may
            load the data payload of data variables into memory if they are not
            already loaded.
          * "all": All data variables will be concatenated.
          * None: Means ``"all"`` if ``dim`` is not present in any of the ``objs``,
            and ``"minimal"`` if ``dim`` is present in any of ``objs``.
          * list of str: The listed data variables will be concatenated, in
            addition to the "minimal" data variables.

        If objects are DataArrays, data_vars must be "all" or None.
    coords : {"minimal", "different", "all"} or list of str, default: "different"
        These coordinate variables will be concatenated together:
          * "minimal": Only coordinates in which the dimension already appears
            are included.
          * "different": Coordinates which are not equal (ignoring attributes)
            across all datasets are also concatenated (as well as all for which
            dimension already appears). Beware: this option may load the data
            payload of coordinate variables into memory if they are not already
            loaded.
          * "all": All coordinate variables will be concatenated, except
            those corresponding to other dimensions.
          * list of str: The listed coordinate variables will be concatenated,
            in addition to the "minimal" coordinates.
    compat : {"identical", "equals", "broadcast_equals", "no_conflicts", "override"}, \
             default: "equals"
        String indicating how to compare non-concatenated variables of the same name for
        potential conflicts. This is passed down to merge.

        - "broadcast_equals": all values must be equal when variables are
          broadcast against each other to ensure common dimensions.
        - "equals": all values and dimensions must be the same.
        - "identical": all values, dimensions and attributes must be the
          same.
        - "no_conflicts": only values which are not null in both datasets
          must be equal. The returned dataset then contains the combination
          of all non-null values.
        - "override": skip comparing and pick variable from first dataset
    positions : None or list of integer arrays, optional
        List of integer arrays which specifies the integer positions to which
        to assign each dataset along the concatenated dimension. If not
        supplied, objects are concatenated in the provided order.
    fill_value : scalar or dict-like, optional
        Value to use for newly missing values. If a dict-like, maps
        variable names to fill values. Use a data array's name to
        refer to its values.
    join : {"outer", "inner", "left", "right", "exact"}, default: "outer"
        String indicating how to combine differing indexes
        (excluding dim) in objects

        - "outer": use the union of object indexes
        - "inner": use the intersection of object indexes
        - "left": use indexes from the first object with each dimension
        - "right": use indexes from the last object with each dimension
        - "exact": instead of aligning, raise `ValueError` when indexes to be
          aligned are not equal
        - "override": if indexes are of same size, rewrite indexes to be
          those of the first object with that dimension. Indexes for the same
          dimension must have the same size in all objects.
    combine_attrs : {"drop", "identical", "no_conflicts", "drop_conflicts", \
                     "override"} or callable, default: "override"
        A callable or a string indicating how to combine attrs of the objects being
        merged:

        - "drop": empty attrs on returned Dataset.
        - "identical": all attrs must be the same on every object.
        - "no_conflicts": attrs from all objects are combined, any that have
          the same name must also have the same value.
        - "drop_conflicts": attrs from all objects are combined, any that have
          the same name but different values are dropped.
        - "override": skip comparing and copy attrs from the first dataset to
          the result.

        If a callable, it must expect a sequence of ``attrs`` dicts and a context object
        as its only parameters.
    create_index_for_new_dim : bool, default: True
        Whether to create a new ``PandasIndex`` object when the objects being
        concatenated contain scalar variables named ``dim``.

    Returns
    -------
    concatenated : type of objs

    See Also
    --------
    merge

    Examples
    --------
    >>> da = xr.DataArray(
    ...     np.arange(6).reshape(2, 3), [("x", ["a", "b"]), ("y", [10, 20, 30])]
    ... )
    >>> da
    <xarray.DataArray (x: 2, y: 3)> Size: 48B
    array([[0, 1, 2],
           [3, 4, 5]])
    Coordinates:
      * x        (x) <U1 8B 'a' 'b'
      * y        (y) int64 24B 10 20 30

    >>> xr.concat([da.isel(y=slice(0, 1)), da.isel(y=slice(1, None))], dim="y")
    <xarray.DataArray (x: 2, y: 3)> Size: 48B
    array([[0, 1, 2],
           [3, 4, 5]])
    Coordinates:
      * x        (x) <U1 8B 'a' 'b'
      * y        (y) int64 24B 10 20 30

    >>> xr.concat([da.isel(x=0), da.isel(x=1)], "x", coords="minimal")
    <xarray.DataArray (x: 2, y: 3)> Size: 48B
    array([[0, 1, 2],
           [3, 4, 5]])
    Coordinates:
      * x        (x) <U1 8B 'a' 'b'
      * y        (y) int64 24B 10 20 30

    >>> xr.concat([da.isel(x=0), da.isel(x=1)], "new_dim", coords="all")
    <xarray.DataArray (new_dim: 2, y: 3)> Size: 48B
    array([[0, 1, 2],
           [3, 4, 5]])
    Coordinates:
        x        (new_dim) <U1 8B 'a' 'b'
      * y        (y) int64 24B 10 20 30
    Dimensions without coordinates: new_dim

    >>> xr.concat(
    ...     [da.isel(x=0), da.isel(x=1)],
    ...     pd.Index([-90, -100], name="new_dim"),
    ...     coords="all",
    ... )
    <xarray.DataArray (new_dim: 2, y: 3)> Size: 48B
    array([[0, 1, 2],
           [3, 4, 5]])
    Coordinates:
      * new_dim  (new_dim) int64 16B -90 -100
        x        (new_dim) <U1 8B 'a' 'b'
      * y        (y) int64 24B 10 20 30

    # Concatenate a scalar variable along a new dimension of the same name with and without creating a new index

    >>> ds = xr.Dataset(coords={"x": 0})
    >>> xr.concat([ds, ds], dim="x")
    <xarray.Dataset> Size: 16B
    Dimensions:  (x: 2)
    Coordinates:
      * x        (x) int64 16B 0 0
    Data variables:
        *empty*

    >>> xr.concat([ds, ds], dim="x").indexes
    Indexes:
        x        Index([0, 0], dtype='int64', name='x')

    >>> xr.concat([ds, ds], dim="x", create_index_for_new_dim=False).indexes
    Indexes:
        *empty*
    """
    # TODO: add ignore_index arguments copied from pandas.concat
    # TODO: support concatenating scalar coordinates even if the concatenated
    # dimension already exists
    from xarray.core.dataarray import DataArray
    from xarray.core.dataset import Dataset
    from xarray.core.datatree import DataTree

    try:
        first_obj, objs = utils.peek_at(objs)
    except StopIteration as err:
        raise ValueError("must supply at least one object to concatenate") from err

    if not isinstance(compat, CombineKwargDefault) and compat not in set(
        _VALID_COMPAT
    ) - {"minimal"}:
        raise ValueError(
            f"compat={compat!r} invalid: must be 'broadcast_equals', 'equals', 'identical', 'no_conflicts' or 'override'"
        )

    if isinstance(first_obj, DataTree):
        return _datatree_concat(
            objs,
            dim=dim,
            data_vars=data_vars,
            coords=coords,
            compat=compat,
            positions=positions,
            fill_value=fill_value,
            join=join,
            combine_attrs=combine_attrs,
            create_index_for_new_dim=create_index_for_new_dim,
        )
    elif isinstance(first_obj, DataArray):
        return _dataarray_concat(
            objs,
            dim=dim,
            data_vars=data_vars,
            coords=coords,
            compat=compat,
            positions=positions,
            fill_value=fill_value,
            join=join,
            combine_attrs=combine_attrs,
            create_index_for_new_dim=create_index_for_new_dim,
        )
    elif isinstance(first_obj, Dataset):
        return _dataset_concat(
            objs,
            dim=dim,
            data_vars=data_vars,
            coords=coords,
            compat=compat,
            positions=positions,
            fill_value=fill_value,
            join=join,
            combine_attrs=combine_attrs,
            create_index_for_new_dim=create_index_for_new_dim,
        )
    else:
        raise TypeError(
            "can only concatenate xarray Dataset and DataArray "
            f"objects, got {type(first_obj)}"
        )


def _calc_concat_dim_index(
    dim_or_data: Hashable | Any,
) -> tuple[Hashable, PandasIndex | None]:
    """Infer the dimension name and 1d index for concatenating along the new dimension.

    The 1d index / coordinate variable is only inferred if appropriate.
    """
    from xarray.core.dataarray import DataArray

    dim: Hashable | None

    if utils.hashable(dim_or_data):
        dim = dim_or_data
        index = None
    else:
        if not isinstance(dim_or_data, DataArray | Variable):
            dim = getattr(dim_or_data, "name", None)
            if dim is None:
                dim = "concat_dim"
        else:
            (dim,) = dim_or_data.dims
        coord_dtype = getattr(dim_or_data, "dtype", None)
        if isinstance(coord_dtype, pd.StringDtype):
            # pandas 3 gives a string pd.Index a StringDtype, which is not a
            # dtype PandasIndex would pick for itself, so map it to the numpy
            # string dtype the same way PandasIndexingAdapter does (GH#11317)
            coord_dtype = np.dtypes.StringDType(na_object=coord_dtype.na_value)
        index = PandasIndex(dim_or_data, dim, coord_dtype=coord_dtype)

    return dim, index


def _calc_concat_over(
    datasets: list[T_Dataset],
    dim: Hashable,
    all_dims: set[Hashable],
    data_vars: T_DataVars | Iterable[Hashable] | CombineKwargDefault,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault,
    compat: CompatOptions | CombineKwargDefault,
) -> tuple[set[Hashable], dict[Hashable, bool], list[int], set[Hashable]]:
    """Determine which dataset variables need to be concatenated in the result."""
    # variables to be concatenated
    concat_over = set()
    # variables checked for equality
    equals: dict[Hashable, bool] = {}
    # skip merging these variables.
    #   if concatenating over a dimension 'x' that is associated with an index over 2 variables,
    #   'x' and 'y', then we assert join="equals" on `y` and don't need to merge it.
    #   that assertion happens in the align step prior to this function being called
    skip_merge: set[Hashable] = set()

    if dim in all_dims:
        concat_over_existing_dim = True
        concat_over.add(dim)
    else:
        concat_over_existing_dim = False

    if data_vars == "minimal" and coords == "minimal" and not concat_over_existing_dim:
        raise ValueError(
            "Cannot specify both data_vars='minimal' and coords='minimal' when "
            "concatenating over a new dimension."
        )

    if data_vars is None or (
        isinstance(data_vars, CombineKwargDefault) and data_vars._value is None
    ):
        data_vars = "minimal" if concat_over_existing_dim else "all"

    concat_dim_lengths = []
    for ds in datasets:
        if concat_over_existing_dim and dim not in ds.dims and dim in ds:
            ds = ds.set_coords(dim)
        concat_over.update(k for k, v in ds.variables.items() if dim in v.dims)
        for _, idx_vars in ds.xindexes.group_by_index():
            if any(dim in v.dims for v in idx_vars.values()):
                skip_merge.update(idx_vars.keys())
        concat_dim_lengths.append(ds.sizes.get(dim, 1))

    def process_subset_opt(
        opt: ConcatOptions | Iterable[Hashable] | CombineKwargDefault,
        subset: Literal["coords", "data_vars"],
    ) -> None:
        original = set(concat_over)
        compat_str = (
            compat._value if isinstance(compat, CombineKwargDefault) else compat
        )
        assert compat_str is not None
        if isinstance(opt, str | CombineKwargDefault):
            if opt == "different":
                if isinstance(compat, CombineKwargDefault) and compat != "override":
                    if not isinstance(opt, CombineKwargDefault):
                        emit_user_level_warning(
                            compat.warning_message(
                                "This change will result in the following ValueError: "
                                f"Cannot specify both {subset}='different' and compat='override'.",
                                recommend_set_options=False,
                            ),
                            FutureWarning,
                        )

                if compat == "override":
                    raise ValueError(
                        f"Cannot specify both {subset}='different' and compat='override'."
                        + (
                            compat.error_message()
                            if isinstance(compat, CombineKwargDefault)
                            else ""
                        )
                    )
                # all nonindexes that are not the same in each dataset
                for k in getattr(datasets[0], subset):
                    if k not in concat_over:
                        equal = None

                        variables = [
                            ds.variables[k] for ds in datasets if k in ds.variables
                        ]

                        if len(variables) == 1:
                            # coords="different" doesn't make sense when only one object
                            # contains a particular variable.
                            break
                        elif len(variables) != len(datasets) and opt == "different":
                            raise ValueError(
                                f"{k!r} not present in all datasets and coords='different'. "
                                f"Either add {k!r} to datasets where it is missing or "
                                "specify coords='minimal'."
                            )

                        # first check without comparing values i.e. no computes
                        for var in variables[1:]:
                            equal = getattr(variables[0], compat_str)(
                                var, equiv=lazy_array_equiv
                            )
                            if equal is not True:
                                # exit early if we know these are not equal or that
                                # equality cannot be determined i.e. one or all of
                                # the variables wraps a numpy array
                                break

                        if equal is False:
                            concat_over.add(k)

                        elif equal is None:
                            # Compare the variable of all datasets vs. the one
                            # of the first dataset. Perform the minimum amount of
                            # loads in order to avoid multiple loads from disk
                            # while keeping the RAM footprint low.
                            v_lhs = datasets[0].variables[k].load()
                            # We'll need to know later on if variables are equal.
                            computed = []
                            for ds_rhs in datasets[1:]:
                                v_rhs = ds_rhs.variables[k].compute()
                                computed.append(v_rhs)
                                if not getattr(v_lhs, compat_str)(v_rhs):
                                    concat_over.add(k)
                                    equals[k] = False
                                    # computed variables are not to be re-computed
                                    # again in the future
                                    for ds, v in zip(
                                        datasets[1:], computed, strict=False
                                    ):
                                        ds.variables[k].data = v.data
                                    break
                            else:
                                equal = True
                        if TYPE_CHECKING:
                            assert equal is not None
                        equals[k] = equal

            elif opt == "all":
                concat_over.update(
                    set().union(
                        *[set(getattr(d, subset)) - set(d.dims) for d in datasets]
                    )
                )
            elif opt == "minimal":
                pass
            else:
                raise ValueError(f"unexpected value for {subset}: {opt}")

            if (
                isinstance(opt, CombineKwargDefault)
                and opt._value is not None
                and original != concat_over
                and concat_over_existing_dim
            ):
                warnings.append(
                    opt.warning_message(
                        "This is likely to lead to different results when multiple datasets "
                        "have matching variables with overlapping values.",
                    )
                )
        else:
            valid_vars = tuple(getattr(datasets[0], subset))
            invalid_vars = [k for k in opt if k not in valid_vars]
            if invalid_vars:
                if subset == "coords":
                    raise ValueError(
                        f"the variables {invalid_vars} in coords are not "
                        f"found in the coordinates of the first dataset {valid_vars}"
                    )
                else:
                    # note: data_vars are not listed in the error message here,
                    # because there may be lots of them
                    raise ValueError(
                        f"the variables {invalid_vars} in data_vars are not "
                        f"found in the data variables of the first dataset"
                    )
            concat_over.update(opt)

    warnings: list[str] = []
    process_subset_opt(data_vars, "data_vars")
    process_subset_opt(coords, "coords")

    for warning in warnings:
        emit_user_level_warning(warning, FutureWarning)

    return concat_over, equals, concat_dim_lengths, skip_merge


# Device label columns are written by two generations of exporters: the newer
# one emits uint64 while the older one stores the same non-negative labels as
# int64. Mixing both in one concatenation would promote the result to float64
# (losing precision above 2**53), so such columns are handled as exact integers
# and converted back to uint64. Variables that do not mix the two dtypes keep
# xarray's usual type promotion rules.
_LABEL_ITEM_SIZE = 8


class _MissingLabel:
    """Sentinel marking positions that were added while aligning labels."""

    def __repr__(self) -> str:
        return "<missing label>"


_MISSING_LABEL = _MissingLabel()
_MISSING_LABEL_FILL = np.array(_MISSING_LABEL, dtype=object)


def _is_uint64_dtype(dtype: np.dtype) -> bool:
    return dtype.kind == "u" and dtype.itemsize == _LABEL_ITEM_SIZE


def _is_int64_dtype(dtype: np.dtype) -> bool:
    return dtype.kind == "i" and dtype.itemsize == _LABEL_ITEM_SIZE


def _label_fill_value(fill_value: Any, name: Hashable) -> Any:
    if is_dict_like(fill_value):
        return fill_value.get(name, dtypes.NA)
    return fill_value


def _mixed_label_names(datasets: list[T_Dataset], dim_name: Hashable) -> set[Hashable]:
    """Names of non-index variables that mix uint64 and int64 and get concatenated."""
    concat_over_existing_dim = any(dim_name in ds.dims for ds in datasets)
    dtypes_by_name: dict[Hashable, set[np.dtype]] = {}
    for ds in datasets:
        for name, var in ds.variables.items():
            if name in ds._indexes:
                # Index coordinates cannot be represented as object arrays.
                continue
            if concat_over_existing_dim and dim_name not in var.dims:
                # This variable is not concatenated along ``dim_name``; leave it
                # to the usual merge/promotion rules.
                continue
            dtypes_by_name.setdefault(name, set()).add(var.dtype)

    return {
        name
        for name, dtypes in dtypes_by_name.items()
        if any(_is_uint64_dtype(dtype) for dtype in dtypes)
        and any(_is_int64_dtype(dtype) for dtype in dtypes)
    }


def _fill_value_with_missing_labels(
    fill_value: Any,
    names: Iterable[Hashable],
    mixed_labels: set[Hashable],
) -> dict[Hashable, Any]:
    """Build a per-variable fill mapping, marking mixed label gaps specially.

    Mixed label gaps get ``_MISSING_LABEL`` instead of the user's fill so that
    an invalid fill value only raises when a gap actually has to be filled.
    Every other variable keeps the user-supplied fill (a scalar applies to all
    names, a mapping is looked up by name as usual).
    """
    result: dict[Hashable, Any] = {}
    for name in names:
        if name in mixed_labels:
            result[name] = _MISSING_LABEL_FILL
        elif is_dict_like(fill_value):
            result[name] = fill_value.get(name, dtypes.NA)
        else:
            result[name] = fill_value
    return result


def _to_object_dtype(values: Any) -> np.typing.NDArray[np.object_]:
    return np.asarray(values).astype(object)


def _as_exact_integer_labels(var: Variable) -> Variable:
    """Store 64-bit integer labels as Python ints so no value gets wrapped."""
    from xarray.coding.common import lazy_elemwise_func

    if var.dtype.kind not in "iu":
        return var
    if is_chunked_array(var.data):
        data = lazy_elemwise_func(var.data, _to_object_dtype, np.dtype(object))
        return var._replace(data=data)
    return var._replace(data=var.data.astype(object))


def _as_exact_integer_labels_in(ds: T_Dataset, names: Iterable[Hashable]) -> T_Dataset:
    variables = dict(ds.variables)
    changed = False
    for name in names:
        var = variables.get(name)
        if var is None:
            continue
        new_var = _as_exact_integer_labels(var)
        if new_var is not var:
            variables[name] = new_var
            changed = True
    if not changed:
        return ds
    return ds._replace(variables=variables)


def _check_label_fill_value(fill_value: Any, name: Hashable) -> int | None:
    """Return ``fill_value`` as an int, or raise if it cannot fill uint64."""
    if fill_value is dtypes.NA or fill_value is None:
        return None
    if isinstance(fill_value, float) and np.isnan(fill_value):
        raise ValueError(
            f"fill_value {fill_value!r} is not valid for label variable "
            f"{name!r}: labels are stored as uint64 and cannot hold missing "
            "values, provide a non-negative integer fill_value"
        )
    if isinstance(fill_value, bool) or not isinstance(fill_value, int | np.integer):
        raise ValueError(
            f"fill_value {fill_value!r} is not valid for label variable "
            f"{name!r}: labels are stored as uint64 and need a non-negative "
            "integer fill_value"
        )
    value = int(fill_value)
    if value < 0:
        raise ValueError(
            f"fill_value {fill_value!r} is not valid for label variable "
            f"{name!r}: labels are stored as uint64 and cannot be negative"
        )
    if value > np.iinfo(np.uint64).max:
        raise ValueError(
            f"fill_value {fill_value!r} is not valid for label variable "
            f"{name!r}: labels are stored as uint64 and it does not fit in "
            "that range"
        )
    return value


def _format_label_location(
    position: tuple[int, ...],
    dims: tuple[Hashable, ...],
    coord_values: tuple[Any, ...],
) -> str:
    parts = []
    for dim, values, index in zip(dims, coord_values, position, strict=False):
        if values is not None and index < len(values):
            label = values[index]
            label = label.item() if isinstance(label, np.generic) else label
            parts.append(f"{dim}={label!r}")
        else:
            parts.append(f"{dim} position {index}")
    return ", ".join(parts) if parts else "the concatenated values"


def _labels_to_uint64(
    values: Any,
    name: Hashable,
    fill_value: Any,
    dims: tuple[Hashable, ...],
    coord_values: tuple[Any, ...],
) -> np.typing.NDArray[np.uint64]:
    """Replace missing-label sentinels and convert labels to uint64.

    Negative entries that survive (i.e. are real records, not fill values) are
    reported as invalid labels. Because this runs on the materialized values it
    raises identically for eagerly loaded and chunked data.
    """
    array = np.asarray(values, dtype=object)
    # Coordinate labels are only meaningful if the block still spans the whole
    # dimension (e.g. after slicing the caller may see a sub-range).
    coords = tuple(
        coord if coord is not None and len(coord) == size else None
        for coord, size in zip(coord_values, array.shape, strict=False)
    )
    flat = array.ravel()
    used_fill = any(isinstance(value, _MissingLabel) for value in flat)
    if used_fill:
        replacement = _check_label_fill_value(fill_value, name)
        if replacement is None:
            raise ValueError(
                f"label variable {name!r} needs a fill_value, but none that is "
                "valid for uint64 labels was provided"
            )
        missing = np.array(
            [isinstance(value, _MissingLabel) for value in flat], dtype=bool
        ).reshape(array.shape)
        array = np.where(missing, np.asarray(replacement, dtype=object), array)

    # Validate the values (including any replacement fill) before casting.
    for value, raw_position in zip(
        array.ravel(), np.ndindex(*array.shape), strict=True
    ):
        if isinstance(value, bool):
            continue
        if isinstance(value, float) and np.isnan(value):
            location = _format_label_location(raw_position, dims, coords)
            raise ValueError(
                f"label variable {name!r} contains a missing value at "
                f"{location}, which cannot be represented as uint64; provide "
                "an integer fill_value"
            )
        if isinstance(value, int | np.integer) and value < 0:
            location = _format_label_location(raw_position, dims, coords)
            raise ValueError(
                f"label variable {name!r} contains the invalid negative value "
                f"{value!r} at {location}, which cannot be represented as "
                "uint64"
            )
    return array.astype(np.uint64)


def _finalize_label_variable(
    var: Variable,
    name: Hashable,
    fill_value: Any,
    coordinates: Mapping[Hashable, Any],
) -> Variable:
    """Convert an exact-integer label column back to uint64, validating values.

    Validation happens when the values are materialized, not while building the
    result. That gives eagerly loaded and chunked inputs identical behaviour
    (the same labels or the same error when taking the result's values) and
    keeps valid chunked data readable on demand.
    """
    from xarray.coding.common import lazy_elemwise_func

    coord_values = tuple(
        None if dim not in coordinates else np.asarray(coordinates[dim])
        for dim in var.dims
    )
    func = functools.partial(
        _labels_to_uint64,
        name=name,
        fill_value=fill_value,
        dims=var.dims,
        coord_values=coord_values,
    )
    return var._replace(data=lazy_elemwise_func(var.data, func, np.uint64))


# determine dimensional coordinate names and a dict mapping name to DataArray
def _parse_datasets(
    datasets: list[T_Dataset],
) -> tuple[
    set[Hashable],
    dict[Hashable, Variable],
    dict[Hashable, int],
    set[Hashable],
    set[Hashable],
    list[Hashable],
]:
    dims: set[Hashable] = set()
    all_coord_names: set[Hashable] = set()
    data_vars: set[Hashable] = set()  # list of data_vars
    dim_coords: dict[Hashable, Variable] = {}  # maps dim name to variable
    dims_sizes: dict[Hashable, int] = {}  # shared dimension sizes to expand variables
    variables_order: dict[Hashable, Variable] = {}  # variables in order of appearance

    for ds in datasets:
        dims_sizes.update(ds.sizes)
        all_coord_names.update(ds.coords)
        data_vars.update(ds.data_vars)
        variables_order.update(ds.variables)

        # preserves ordering of dimensions
        for dim in ds.dims:
            if dim in dims:
                continue

            if dim in ds.coords and dim not in dim_coords:
                dim_coords[dim] = ds.coords[dim].variable
        dims = dims | set(ds.dims)

    return (
        dims,
        dim_coords,
        dims_sizes,
        all_coord_names,
        data_vars,
        list(variables_order),
    )


def _dataset_concat(
    datasets: Iterable[T_Dataset],
    dim: Hashable | T_Variable | T_DataArray | pd.Index,
    data_vars: T_DataVars | CombineKwargDefault,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault,
    compat: CompatOptions | CombineKwargDefault,
    positions: Iterable[Iterable[int]] | None,
    fill_value: Any,
    join: JoinOptions | CombineKwargDefault,
    combine_attrs: CombineAttrsOptions,
    create_index_for_new_dim: bool,
    *,
    preexisting_dim: bool = False,
) -> T_Dataset:
    """Concatenate a sequence of datasets along a new or existing dimension."""
    from xarray.core.dataarray import DataArray
    from xarray.core.dataset import Dataset

    datasets = list(datasets)

    if not all(isinstance(dataset, Dataset) for dataset in datasets):
        raise TypeError(
            "The elements in the input list need to be either all 'Dataset's or all 'DataArray's"
        )

    dim_var: Variable | None
    if isinstance(dim, DataArray):
        dim_var = dim.variable
    elif isinstance(dim, Variable):
        dim_var = dim
    else:
        dim_var = None

    dim_name, index = _calc_concat_dim_index(dim)

    # Columns mixing uint64 and int64 labels (the two exporter generations) are
    # risky under xarray's usual promotion: mixed int64/uint64 becomes float64,
    # silently losing precision for labels above 2**53. Keep such columns as
    # exact Python integers through alignment and concatenation, then convert
    # back to uint64 once the result is assembled.
    mixed_labels = _mixed_label_names(datasets, dim_name)
    label_fill_values = {
        name: _label_fill_value(fill_value, name) for name in mixed_labels
    }
    if mixed_labels:
        datasets = [_as_exact_integer_labels_in(ds, mixed_labels) for ds in datasets]
        all_names = set().union(*(ds.variables for ds in datasets))
        align_fill_value: Any = _fill_value_with_missing_labels(
            fill_value, all_names, mixed_labels
        )
    else:
        align_fill_value = fill_value

    # Make sure we're working on a copy (we'll be loading variables)
    datasets = [ds.copy() for ds in datasets]
    datasets = list(
        align(
            *datasets,
            join=join,
            copy=False,
            exclude=[dim_name],
            fill_value=align_fill_value,
        )
    )
    if mixed_labels:
        # Values added by alignment (for stations missing in some datasets) are
        # materialized as Python ints too, so the final conversion sees them.
        datasets = [_as_exact_integer_labels_in(ds, mixed_labels) for ds in datasets]

    all_dims, dim_coords, dims_sizes, coord_names, data_names, vars_order = (
        _parse_datasets(datasets)
    )
    if preexisting_dim:
        # When concatenating DataTree objects, a dimension may be pre-existing
        # because it exists elsewhere on the trees, even if it does not exist
        # on the dataset objects at this node.
        all_dims.add(dim_name)
    indexed_dim_names = set(dim_coords)

    both_data_and_coords = coord_names & data_names
    if both_data_and_coords:
        raise ValueError(
            f"{both_data_and_coords!r} is a coordinate in some datasets but not others."
        )
    # we don't want the concat dimension in the result dataset yet
    dim_coords.pop(dim_name, None)
    dims_sizes.pop(dim_name, None)

    # case where concat dimension is a coordinate or data_var but not a dimension
    if (
        dim_name in coord_names or dim_name in data_names
    ) and dim_name not in indexed_dim_names:
        datasets = [
            ds.expand_dims(dim_name, create_index_for_new_dim=create_index_for_new_dim)
            for ds in datasets
        ]
        all_dims.add(dim_name)
        # This isn't being used any more, but keeping it up to date
        # just in case we decide to use it later.
        indexed_dim_names.add(dim_name)

    # determine which variables to concatenate
    concat_over, equals, concat_dim_lengths, skip_merge = _calc_concat_over(
        datasets, dim_name, all_dims, data_vars, coords, compat
    )

    # determine which variables to merge, and then merge them according to compat
    variables_to_merge = (coord_names | data_names) - concat_over - skip_merge

    result_vars = {}
    result_indexes = {}

    if variables_to_merge:
        grouped = {
            k: v
            for k, v in collect_variables_and_indexes(datasets).items()
            if k in variables_to_merge
        }
        merged_vars, merged_indexes = merge_collected(
            grouped, compat=compat, equals=equals
        )
        result_vars.update(merged_vars)
        result_indexes.update(merged_indexes)

    result_vars.update(dim_coords)

    # assign attrs and encoding from first dataset
    result_attrs = merge_attrs([ds.attrs for ds in datasets], combine_attrs)
    result_encoding = datasets[0].encoding

    # check that global attributes are fixed across all datasets if necessary
    if compat == "identical":
        for ds in datasets[1:]:
            if not utils.dict_equiv(ds.attrs, result_attrs):
                raise ValueError("Dataset global attributes not equal.")

    # we've already verified everything is consistent; now, calculate
    # shared dimension sizes so we can expand the necessary variables
    def ensure_common_dims(vars, concat_dim_lengths):
        # ensure each variable with the given name shares the same
        # dimensions and the same shape for all of them except along the
        # concat dimension
        common_dims = tuple(utils.OrderedSet(d for v in vars for d in v.dims))
        if dim_name not in common_dims:
            common_dims = (dim_name,) + common_dims
        if any(var.dims != common_dims for var in vars):
            vars = _as_shared_dtype(vars)
        for var, dim_len in zip(vars, concat_dim_lengths, strict=True):
            if var.dims != common_dims:
                common_shape = tuple(dims_sizes.get(d, dim_len) for d in common_dims)
                var = var.set_dims(common_dims, common_shape)
            yield var

    # get the indexes to concatenate together, create a PandasIndex
    # for any scalar coordinate variable found with ``name`` matching ``dim``.
    # TODO: depreciate concat a mix of scalar and dimensional indexed coordinates?
    # TODO: (benbovy - explicit indexes): check index types and/or coordinates
    # of all datasets?
    def get_indexes(name):
        for ds in datasets:
            if name in ds._indexes:
                yield ds._indexes[name]
            elif name == dim_name:
                var = ds._variables[name]
                if not var.dims:
                    data = var.set_dims(dim_name).values
                    if create_index_for_new_dim:
                        yield PandasIndex(data, dim_name, coord_dtype=var.dtype)

    # create concatenation index, needed for later reindexing
    file_start_indexes = np.cumulative_sum(concat_dim_lengths, include_initial=True)
    concat_index_size = file_start_indexes[-1]
    variable_index_mask = np.ones(concat_index_size, dtype=bool)
    variable_reindexer = None

    # stack up each variable and/or index to fill-out the dataset (in order)
    # n.b. this loop preserves variable order, needed for groupby.
    ndatasets = len(datasets)
    for name in vars_order:
        if name in concat_over and name not in result_indexes:
            variables = []
            # Initialize the mask to all True then set False if any name is missing in
            # the datasets:
            variable_index_mask.fill(True)
            var_concat_dim_length = []
            for i, ds in enumerate(datasets):
                if name in ds.variables:
                    variables.append(ds[name].variable)
                    var_concat_dim_length.append(concat_dim_lengths[i])
                else:
                    # raise if coordinate not in all datasets
                    if name in coord_names:
                        raise ValueError(
                            f"coordinate {name!r} not present in all datasets."
                        )

                    # Mask out the indexes without the name:
                    start = file_start_indexes[i]
                    end = file_start_indexes[i + 1]
                    variable_index_mask[slice(start, end)] = False

            vars = ensure_common_dims(variables, var_concat_dim_length)

            # Try to concatenate the indexes, concatenate the variables when no index
            # is found on all datasets.
            indexes: list[Index] = list(get_indexes(name))
            if indexes:
                if len(indexes) < ndatasets:
                    raise ValueError(
                        f"{name!r} must have either an index or no index in all datasets, "
                        f"found {len(indexes)}/{len(datasets)} datasets with an index."
                    )
                combined_idx = indexes[0].concat(indexes, dim_name, positions)
                if name in datasets[0]._indexes:
                    idx_vars = datasets[0].xindexes.get_all_coords(name)
                else:
                    # index created from a scalar coordinate
                    idx_vars = {name: datasets[0][name].variable}
                result_indexes.update(dict.fromkeys(idx_vars, combined_idx))
                combined_idx_vars = combined_idx.create_variables(idx_vars)
                for k, v in combined_idx_vars.items():
                    v.attrs = merge_attrs(
                        [ds.variables[k].attrs for ds in datasets],
                        combine_attrs=combine_attrs,
                    )
                    result_vars[k] = v
            else:
                combined_var = concat_vars(
                    vars, dim_name, positions, combine_attrs=combine_attrs
                )
                # reindex if variable is not present in all datasets
                if not variable_index_mask.all():
                    if variable_reindexer is None:
                        # allocate only once
                        variable_reindexer = np.empty(
                            concat_index_size,
                            # cannot use uint since we need -1 as a sentinel for reindexing
                            dtype=np.min_scalar_type(-concat_index_size),
                        )
                    np.cumsum(variable_index_mask, out=variable_reindexer)
                    # variable_index_mask is boolean, so the first element is 1.
                    # offset by 1 to start at 0.
                    variable_reindexer -= 1
                    variable_reindexer[~variable_index_mask] = -1
                    combined_var = reindex_variables(
                        variables={name: combined_var},
                        dim_pos_indexers={dim_name: variable_reindexer},
                        fill_value=(
                            _MISSING_LABEL_FILL if name in mixed_labels else fill_value
                        ),
                    )[name]
                result_vars[name] = combined_var

        elif name in result_vars:
            # preserves original variable order
            result_vars[name] = result_vars.pop(name)

    absent_coord_names = coord_names - set(result_vars)
    if absent_coord_names:
        raise ValueError(
            f"Variables {absent_coord_names!r} are coordinates in some datasets but not others."
        )

    result_data_vars = {}
    coord_vars = {}
    for name, result_var in result_vars.items():
        if name in coord_names:
            coord_vars[name] = result_var
        else:
            result_data_vars[name] = result_var

    if index is not None:
        if dim_var is not None:
            index_vars = index.create_variables({dim_name: dim_var})
        else:
            index_vars = index.create_variables()

        coord_vars[dim_name] = index_vars[dim_name]
        result_indexes[dim_name] = index

    coords_obj = Coordinates(coord_vars, indexes=result_indexes)

    result = type(datasets[0])(result_data_vars, coords=coords_obj, attrs=result_attrs)
    result.encoding = result_encoding

    if mixed_labels:
        coordinates = {
            dim: result.variables[dim].values
            for dim in result.dims
            if dim in result.variables
        }
        for name in mixed_labels & set(result._variables):
            result._variables[name] = _finalize_label_variable(
                result._variables[name], name, label_fill_values[name], coordinates
            )

    return result


def _dataarray_concat(
    arrays: Iterable[T_DataArray],
    dim: Hashable | T_Variable | T_DataArray | pd.Index,
    data_vars: T_DataVars | Iterable[Hashable] | CombineKwargDefault,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault,
    compat: CompatOptions | CombineKwargDefault,
    positions: Iterable[Iterable[int]] | None,
    fill_value: object,
    join: JoinOptions | CombineKwargDefault,
    combine_attrs: CombineAttrsOptions,
    create_index_for_new_dim: bool,
) -> T_DataArray:
    from xarray.core.dataarray import DataArray

    arrays = list(arrays)

    if not all(isinstance(array, DataArray) for array in arrays):
        raise TypeError(
            "The elements in the input list need to be either all 'Dataset's or all 'DataArray's"
        )

    # Allow passing `all` or `None` even though we always use `data_vars='all'`
    # when passing off to `_dataset_concat`.
    if not isinstance(data_vars, CombineKwargDefault) and data_vars not in [
        "all",
        None,
    ]:
        raise ValueError(
            "data_vars is not a valid argument when concatenating DataArray objects"
        )

    datasets = []
    for n, arr in enumerate(arrays):
        if n == 0:
            name = arr.name
        elif name != arr.name:
            if compat == "identical":
                raise ValueError("array names not identical")
            else:
                arr = arr.rename(name)
        datasets.append(arr._to_temp_dataset())

    ds = _dataset_concat(
        datasets,
        dim=dim,
        data_vars="all",
        coords=coords,
        compat=compat,
        positions=positions,
        fill_value=fill_value,
        join=join,
        combine_attrs=combine_attrs,
        create_index_for_new_dim=create_index_for_new_dim,
    )

    merged_attrs = merge_attrs([da.attrs for da in arrays], combine_attrs)

    result = arrays[0]._from_temp_dataset(ds, name)
    result.attrs = merged_attrs

    return result


def _datatree_concat(
    objs: Iterable[DataTree],
    dim: Hashable | Variable | T_DataArray | pd.Index | Any,
    data_vars: T_DataVars | Iterable[Hashable] | CombineKwargDefault,
    coords: ConcatOptions | Iterable[Hashable] | CombineKwargDefault,
    compat: CompatOptions | CombineKwargDefault,
    positions: Iterable[Iterable[int]] | None,
    fill_value: Any,
    join: JoinOptions | CombineKwargDefault,
    combine_attrs: CombineAttrsOptions,
    create_index_for_new_dim: bool,
) -> DataTree:
    """Concatenate a sequence of datatrees along a new or existing dimension."""
    from xarray.core.datatree import DataTree
    from xarray.core.treenode import TreeIsomorphismError, group_subtrees

    dim_name, _ = _calc_concat_dim_index(dim)

    objs = list(objs)
    if not all(isinstance(obj, DataTree) for obj in objs):
        raise TypeError("All objects to concatenate must be DataTree objects")

    if compat == "identical":
        if any(obj.name != objs[0].name for obj in objs[1:]):
            raise ValueError("DataTree names not identical")

    dim_in_tree = any(dim_name in node.dims for node in objs[0].subtree)

    results = {}
    try:
        for path, nodes in group_subtrees(*objs):
            datasets_to_concat = [node.to_dataset() for node in nodes]
            results[path] = _dataset_concat(
                datasets_to_concat,
                dim=dim,
                data_vars=data_vars,
                coords=coords,
                compat=compat,
                positions=positions,
                fill_value=fill_value,
                join=join,
                combine_attrs=combine_attrs,
                create_index_for_new_dim=create_index_for_new_dim,
                preexisting_dim=dim_in_tree,
            )
    except TreeIsomorphismError as e:
        raise ValueError("All trees must be isomorphic to be concatenated") from e

    return DataTree.from_dict(results, name=objs[0].name)
