/*
 * Copyright 2015 VMware, Inc. All Rights Reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License"); you may not
 * use this file except in compliance with the License.  You may obtain a copy of
 * the License at http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software distributed
 * under the License is distributed on an "AS IS" BASIS, without warranties or
 * conditions of any kind, EITHER EXPRESS OR IMPLIED.  See the License for the
 * specific language governing permissions and limitations under the License.
 */

package com.vmware.photon.controller.apife.lib.ova;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;
import java.io.InputStream;

/**
 * Class encapsulating VMDK metadata elements.
 */
public class VmdkMetadata {

  private static final Logger logger = LoggerFactory.getLogger(VmdkMetadata.class);
  private static final String EXTENT_DESCRIPTION_HEADER = "# Extent description";
  private static final String DISK_DATA_BASE_HEADER = "# The Disk Data Base";

  /**
   * Get the size of sector -> extent.
   * For the following example, the function returns 4192256.
   * If data is split across multiple data files as multiple vmdks, we consider it invalid vmdk.
   * <p/>
   * # Extent description
   * RW 4192256 SPARSE "test-s001.vmdk"
   * # The Disk Data Bas
   *
   * @param inputStream
   * @return
   * @throws java.io.IOException
   */
  public static int getSingleExtentSize(InputStream inputStream)
      throws IOException {
    String s = DataField.getString(new DataField(0, 100000), inputStream);
    int beg = s.indexOf(EXTENT_DESCRIPTION_HEADER);
    if (beg < 0) {
      logger.error("Invalid vmdk: missing {}\n{}", EXTENT_DESCRIPTION_HEADER, s);
      throw new IllegalStateException("Invalid vmdk: missing " + EXTENT_DESCRIPTION_HEADER);
    }
    int end = s.indexOf(DISK_DATA_BASE_HEADER, beg);
    if (end < 0) {
      logger.error("Invalid vmdk: missing {}\n{}", DISK_DATA_BASE_HEADER, s);
      throw new IllegalStateException("Invalid vmdk: missing " + DISK_DATA_BASE_HEADER);
    }
    s = s.substring(beg + EXTENT_DESCRIPTION_HEADER.length(), end).trim();
    if (s.indexOf("\n") >= 0) {
      logger.error("Invalid vmdk: multiple vmdks detected\n{}", s);
      throw new IllegalStateException("Invalid vmdk: multiple vmdks detected");
    }
    String[] fields = s.split(" ");
    if (fields.length != 4) {
      logger.error("Invalid vmdk: fields length is {}\n{}", fields.length, s);
      throw new IllegalStateException("Invalid vmdk: fields length is " + fields.length);
    }

    try {
      return Integer.parseInt(fields[1]);
    } catch (NumberFormatException e) {
      logger.error("Invalid string: second field is not integer\n{}", s);
      throw new IllegalStateException("Invalid string: second field is not integer", e);
    }
  }

}
