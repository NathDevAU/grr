#!/usr/bin/env python
"""These are low-level related flows."""



from grr.client.client_actions import tempfiles as tempfiles_actions

# The DumpFlashImage class is loaded from here
from grr.client.components.chipsec_support import grr_chipsec_stub

from grr.lib import aff4
from grr.lib import flow
from grr.lib.aff4_objects import hardware
from grr.lib.flows.general import transfer
from grr.lib.rdfvalues import structs as rdf_structs
from grr.proto import flows_pb2


class DumpFlashImageArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.DumpFlashImageArgs


class DumpFlashImage(transfer.LoadComponentMixin, flow.GRRFlow):
  """Dump Flash image (BIOS)."""

  category = "/Collectors/"
  behaviours = flow.GRRFlow.behaviours + "BASIC"
  args_type = DumpFlashImageArgs

  @flow.StateHandler()
  def Start(self):
    """Load grr_chipsec component on the client."""
    self.LoadComponentOnClient(
        name="grr-chipsec-component",
        version=self.args.component_version,
        next_state="CollectDebugInfo")

  @flow.StateHandler()
  def CollectDebugInfo(self, responses):
    """Start by collecting general hardware information."""
    self.CallFlow(
        "ArtifactCollectorFlow",
        artifact_list=["LinuxHardwareInfo"],
        store_results_in_aff4=True,
        next_state="DumpImage")

  @flow.StateHandler()
  def DumpImage(self, responses):
    """Store hardware information and initiate dumping of the flash image."""
    self.state.hardware_info = responses.First()
    self.CallClient(
        grr_chipsec_stub.DumpFlashImage,
        log_level=self.args.log_level,
        chunk_size=self.args.chunk_size,
        notify_syslog=self.args.notify_syslog,
        next_state="CollectImage")

  @flow.StateHandler()
  def CollectImage(self, responses):
    """Collect the image and store it into the database."""
    # If we have any log, forward them.
    for response in responses:
      if hasattr(response, "logs"):
        for log in response.logs:
          self.Log(log)

    if not responses.success:
      raise flow.FlowError("Failed to dump the flash image: {0}".format(
          responses.status))
    elif not responses.First().path:
      self.Log("No path returned. Skipping host.")
      self.CallState(next_state="End")
    else:
      image_path = responses.First().path
      self.CallFlow(
          "MultiGetFile",
          pathspecs=[image_path],
          request_data={"image_path": image_path},
          next_state="DeleteTemporaryImage")

  @flow.StateHandler()
  def DeleteTemporaryImage(self, responses):
    """Remove the temporary image from the client."""
    if not responses.success:
      raise flow.FlowError("Unable to collect the flash image: {0}".format(
          responses.status))

    response = responses.First()
    self.SendReply(response)

    # Update the symbolic link to the new instance.
    with aff4.FACTORY.Create(
        self.client_id.Add("spiflash"), aff4.AFF4Symlink,
        token=self.token) as symlink:
      symlink.Set(symlink.Schema.SYMLINK_TARGET, response.aff4path)

    # Clean up the temporary image from the client.
    self.CallClient(
        tempfiles_actions.DeleteGRRTempFiles,
        responses.request_data["image_path"],
        next_state="TemporaryImageRemoved")

  @flow.StateHandler()
  def TemporaryImageRemoved(self, responses):
    """Verify that the temporary image has been removed successfully."""
    if not responses.success:
      raise flow.FlowError("Unable to delete the temporary flash image: "
                           "{0}".format(responses.status))

  @flow.StateHandler()
  def End(self):
    if hasattr(self.state, "image_path"):
      self.Log("Successfully wrote Flash image.")


class DumpACPITableArgs(rdf_structs.RDFProtoStruct):
  protobuf = flows_pb2.DumpACPITableArgs

  def Validate(self):
    if not self.table_signature_list:
      raise ValueError("No ACPI table to dump.")


class DumpACPITable(transfer.LoadComponentMixin, flow.GRRFlow):
  """Flow to retrieve ACPI tables."""

  category = "/Collectors/"
  behaviours = flow.GRRFlow.behaviours + "BASIC"
  args_type = DumpACPITableArgs

  @flow.StateHandler()
  def Start(self):
    """Load grr-chipsec component on the client."""
    self.LoadComponentOnClient(
        name="grr-chipsec-component",
        version=self.args.component_version,
        next_state="StartCollection")

  @flow.StateHandler()
  def StartCollection(self, responses):
    """Start collecting tables with listed signature."""
    for table_signature in self.args.table_signature_list:
      self.CallClient(
          grr_chipsec_stub.DumpACPITable,
          logging=self.args.logging,
          table_signature=table_signature,
          next_state="TableReceived")

  @flow.StateHandler()
  def TableReceived(self, responses):
    """Store received ACPI tables from client in AFF4."""
    for response in responses:
      for log in response.logs:
        self.Log(log)

    table_signature = responses.request.request.payload.table_signature

    if not responses.success:
      self.Log("Error retrieving ACPI table with signature %s" %
               table_signature)
      return

    response = responses.First()

    if response.acpi_tables:
      self.Log("Retrieved ACPI table(s) with signature %s" % table_signature)
      with aff4.FACTORY.Create(
          self.client_id.Add("devices/chipsec/acpi/tables/%s" %
                             table_signature),
          hardware.ACPITableDataCollection,
          token=self.token) as fd:
        fd.AddAll(response.acpi_tables)
        fd.Flush()

      for acpi_table_response in response.acpi_tables:
        self.SendReply(acpi_table_response)
