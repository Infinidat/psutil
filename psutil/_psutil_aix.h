/*
 * Copyright (c) 2009, Giampaolo Rodola'. All rights reserved.
 * Use of this source code is governed by a BSD-style license that can be
 * found in the LICENSE file.
 */

#include <Python.h>

// processes
static PyObject* psutil_proc_basic_info(PyObject* self, PyObject* args);
static PyObject* psutil_proc_cpu_times(PyObject* self, PyObject* args);
static PyObject* psutil_proc_cred(PyObject* self, PyObject* args);
static PyObject* psutil_proc_name_and_args(PyObject* self, PyObject* args);

// system
static PyObject* psutil_boot_time(PyObject* self, PyObject* args);
static PyObject* psutil_users(PyObject* self, PyObject* args);
static PyObject* psutil_per_cpu_times(PyObject* self, PyObject* args);
static PyObject* psutil_virtual_mem(PyObject* self, PyObject* args);
