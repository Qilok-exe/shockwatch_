"""Scientific-comparison boundaries, not market-performance assertions."""
import copy
import sqlite3
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from shockwatch.common import digest, utc
from shockwatch.experiments import (
    aggregate_daily, block_uncertainty, case_families, fit_stock_model,
    freeze_manifest, measurement_comparison, paired_forecast_losses,
    persist_packet, run_experiment, save_packet, source_hash, packet_completion,
    CANDIDATES, _candidate_scores, _normalize_ledger,
)
from shockwatch.models import enrich
from shockwatch.store import Store
from shockwatch.synthetic import generate


def refreeze(manifest):
    manifest.pop("manifest_id",None)
    manifest["manifest_id"]=digest(manifest)
    return manifest


class ExperimentBoundaries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.panel,cls.calendar,cls.labels=generate(Path(cls.tmp.name)/"inputs",160)
        cls.panel["dataset_id"]="synthetic-tests"
        cls.panel["mode"]="synthetic"
        cls.manifest=freeze_manifest(cls.panel,cls.calendar,cls.labels,dataset_id="synthetic-tests",bootstrap_samples=40)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_training_transforms_and_validation_policy_ignore_test_mutation(self):
        manifest=copy.deepcopy(self.manifest)
        manifest["candidates"]=["stock_only_hourly","full_hourly"]
        refreeze(manifest)
        before=run_experiment(self.panel,self.calendar,self.labels,manifest)
        changed=self.panel.copy()
        test=changed.session.ge(manifest["folds"][0]["test_start"])
        changed.loc[test,"stock_volume"]*=10000
        changed.loc[test,"rv"]*=10000
        changed.loc[test,"iv30"]+=100
        changed_manifest=copy.deepcopy(manifest)
        changed_manifest["source_hashes"]["panel"]=source_hash(changed)
        refreeze(changed_manifest)
        after=run_experiment(changed,self.calendar,self.labels,changed_manifest)
        self.assertEqual(before["folds"][0]["artifacts"],after["folds"][0]["artifacts"])
        for a,b in zip(before["folds"][0]["candidates"],after["folds"][0]["candidates"]):
            self.assertEqual(a["status"],"complete")
            self.assertEqual(a["policy"],b["policy"])
            self.assertEqual(a["policy_calibration_candidates"],b["policy_calibration_candidates"])
        self.assertEqual(before["folds"][0]["purged_training_sessions"],[self.calendar.session.iloc[99]])
        self.assertLess(utc(before["folds"][0]["training_cutoff"]),utc(self.calendar.open_utc.iloc[100]))

    def test_family_crossing_boundary_is_excluded_as_whole(self):
        fold=self.manifest["folds"][0]
        labels=pd.DataFrame([
            {"case_id":"a","event_group":"one-announcement","instrument_id":"UAL","window_start":self.calendar.open_utc.iloc[99],"public_at":self.calendar.open_utc.iloc[101],"status":"synthetic"},
            {"case_id":"b","event_group":"one-announcement","instrument_id":"LMT","window_start":self.calendar.open_utc.iloc[100],"public_at":self.calendar.open_utc.iloc[101],"status":"synthetic"},
        ])
        included,excluded=case_families(labels,fold,self.calendar)
        self.assertEqual(included,[])
        self.assertEqual(len(excluded),1)
        self.assertEqual(excluded[0]["instruments"],["LMT","UAL"])
        self.assertEqual(set(excluded[0]["case_ids"]),{"a","b"})

    def test_unverified_leads_and_mode_mixing_are_rejected(self):
        labels=self.labels.copy();labels.loc[0,"status"]="research_lead"
        manifest=freeze_manifest(self.panel,self.calendar,labels,dataset_id="synthetic-tests",bootstrap_samples=10)
        with self.assertRaisesRegex(ValueError,"Research leads"):
            run_experiment(self.panel,self.calendar,labels,manifest)
        mixed=self.panel.copy();mixed.loc[0,"mode"]="prospective"
        with self.assertRaisesRegex(ValueError,"matching dataset"):
            freeze_manifest(mixed,self.calendar,self.labels,dataset_id="synthetic-tests")

    def test_runtime_secrets_cannot_enter_frozen_export_configuration(self):
        manifest=freeze_manifest(self.panel,self.calendar,self.labels,dataset_id="synthetic-tests",
                                 config={"notifications":{"smtp_password":"example-sensitive-value"},
                                         "private_token":"example-sensitive-value"})
        self.assertNotIn("example-sensitive-value",str(manifest))
        self.assertNotIn("notifications",manifest["fit_configuration"])
        self.assertNotIn("private_token",manifest["fit_configuration"])

    def test_frozen_sources_and_chronology_cannot_change_silently(self):
        changed=self.panel.copy();changed.loc[0,"rv"]*=2
        with self.assertRaisesRegex(ValueError,"source hash"):
            run_experiment(changed,self.calendar,self.labels,self.manifest)
        changed_manifest=copy.deepcopy(self.manifest)
        changed_manifest["folds"][0]["validation_start"]=changed_manifest["folds"][0]["train_end"]
        refreeze(changed_manifest)
        with self.assertRaisesRegex(ValueError,"chronological"):
            run_experiment(self.panel,self.calendar,self.labels,changed_manifest)

    def test_daily_volume_is_once_and_missing_whole_day_remains_excluded(self):
        daily=aggregate_daily(self.panel,self.calendar)
        day=self.calendar.session.iloc[40]  # The fixture's early close.
        source=self.panel[self.panel.instrument_id.eq("SPY")&self.panel.session.eq(day)]
        target=daily[daily.instrument_id.eq("SPY")&daily.session.eq(day)].iloc[0]
        self.assertEqual(target.stock_volume,float(source.stock_volume.sum()))
        self.assertEqual(target.volume_call_7_30,float(source.volume_call_7_30.sum()))
        self.assertEqual(target.minutes,210.)
        self.assertEqual(target.contracts_call_7_30,12)  # Closing population, not summed seven times.
        missing=self.panel[~(self.panel.instrument_id.eq("SPY")&self.panel.session.eq(day))]
        result=aggregate_daily(missing,self.calendar)
        self.assertFalse(((result.instrument_id=="SPY")&(result.session==day)).any())
        partial=self.panel.drop(source.index[0])
        result=aggregate_daily(partial,self.calendar)
        target=result[result.instrument_id.eq("SPY")&result.session.eq(day)].iloc[0]
        self.assertFalse(target.session_complete)
        self.assertTrue(pd.isna(target.volume_call_7_30))
        self.assertFalse(target.flow_eligible)

    def test_stock_only_training_and_signal_ignore_options_quality(self):
        training=enrich(self.panel[self.panel.session<self.calendar.session.iloc[100]])
        before=fit_stock_model(training)
        unavailable=training.copy()
        unavailable["data_ok"]=False
        unavailable["flow_eligible"]=False
        unavailable["price_eligible"]=False
        unavailable["iv30"]=np.nan
        self.assertEqual(before,fit_stock_model(unavailable))
        row=training.iloc[-1].to_dict()
        row["stock_sector_peer_volume"]=np.nan  # Pilot sectors contain one instrument.
        original=_candidate_scores([row],"stock_only_hourly",None,before)[0][1]
        row.update(data_ok=False,flow_eligible=False,price_eligible=False,iv30=np.nan,oi_token=None)
        without_options=_candidate_scores([row],"stock_only_hourly",None,before)[0][1]
        self.assertEqual(original["flow_call"],without_options["flow_call"])
        self.assertTrue(np.isfinite(original["flow_call"]))

    def test_failed_candidate_retained_and_packet_store_is_immutable_and_isolated(self):
        manifest=copy.deepcopy(self.manifest);manifest["candidates"]=["unknown-prespecified-challenger"]
        refreeze(manifest)
        packet=run_experiment(self.panel,self.calendar,self.labels,manifest)
        self.assertEqual(packet["folds"][0]["candidates"][0]["status"],"failed")
        self.assertEqual(packet["mode"],"synthetic")
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"synthetic.db"
            store=Store(path,{"dataset_id":"synthetic-tests","mode":"synthetic"});store.db.close()
            first=persist_packet(path,packet)
            self.assertEqual(first,persist_packet(path,packet))
            store=Store(path)
            self.assertEqual(store.db.execute("SELECT count(*) FROM experiments").fetchone()[0],1)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute("UPDATE experiments SET status='rewritten'")
            store.db.close()
            changed=copy.deepcopy(packet);changed["promotion"]="unauthorized"
            changed.pop("packet_id");changed["packet_id"]=digest(changed)
            exports=Path(tmp)/"packet"
            save_packet(packet,exports)
            save_packet(packet,exports)
            with self.assertRaisesRegex(ValueError,"overwrite a different"):
                save_packet(changed,exports)
            with self.assertRaisesRegex(ValueError,"different results"):
                persist_packet(path,changed)
            wrong=Path(tmp)/"real.db"
            store=Store(wrong,{"dataset_id":"real-tests","mode":"prospective"});store.db.close()
            with self.assertRaisesRegex(ValueError,"does not match"):
                persist_packet(wrong,packet)


class PairedLossAndDependenceTests(unittest.TestCase):
    def records(self):
        forecasts=[];outcomes=[]
        for i,day in enumerate(("2024-01-02","2024-01-03","2024-01-04")):
            for symbol in ("A","B"):
                target=f"{day}-{symbol}"
                common={"target_id":target,"instrument_id":symbol,"session":day,"target_definition":"intraday-v1",
                        "issued_at":day+"T13:00:00Z","target_start":day+"T14:30:00Z","target_end":day+"T21:00:00Z"}
                forecasts.extend([{**common,"model_name":"seasonal_ewma","variance":2.},{**common,"model_name":"daily_log_har","variance":1.}])
                outcomes.append({**common,"status":"complete","realized_variance":1.,"evaluated_at":day+"T21:10:00Z","version":1})
        return forecasts,outcomes

    def test_pairs_use_exact_target_and_incomplete_outcomes_are_visible(self):
        f,o=self.records();o[0]["status"]="incomplete_data";o[0]["realized_variance"]=None
        r=paired_forecast_losses(f,o,samples=20)
        self.assertEqual(r["paired_targets"],5)
        self.assertEqual(r["incomplete_targets"],1)
        self.assertAlmostEqual(r["paired_difference_uncertainty"]["mean"],1.-(np.log(2.)+.5))
        bad=copy.deepcopy(f);bad[0]["issued_at"]="2024-01-02T15:00:00Z"
        with self.assertRaisesRegex(ValueError,"before issuance"):
            paired_forecast_losses(bad,o,samples=20)
        bad=copy.deepcopy(f);bad[1]["target_end"]="2024-01-02T20:00:00Z"
        with self.assertRaisesRegex(ValueError,"contracts disagree"):
            paired_forecast_losses(bad,self.records()[1],samples=20)

    def test_original_ledger_revisions_keep_same_source_and_reject_mixed_provenance(self):
        f,o=self.records()
        records=[];outcomes=[]
        for record in f[:2]:
            method="seasonal_intraday_ewma" if record["model_name"]=="seasonal_ewma" else "intraday_har"
            item={**record,"forecast_id":"f-"+method,"dataset_id":"real-tests","mode":"prospective","run_id":"run",
                  "model_id":"model","policy_id":"policy","feature_version":"0.1.0","target_definition_version":"next_session_intraday_v1",
                  "source_version":"first-release","asof_at":record["issued_at"],"input_watermark":record["issued_at"],
                  "recorded_at":record["issued_at"],"training_cutoff":"2024-01-01T21:00:00Z","horizon_id":"next_session_intraday",
                  "component":"intraday","method":method,"predicted_variance":record["variance"],"forecast_status":"issued","quality_reasons":[]}
            records.append(item)
            outcomes.append({**o[0],"forecast_id":item["forecast_id"],"dataset_id":"real-tests","mode":"prospective",
                             "target_definition_version":"next_session_intraday_v1","observed_variance":1.,"target_coverage":1.,
                             "outcome_available_at":"2024-01-02T21:05:00Z","source_revision":"first_release","source_evidence_hash":"same-path",
                             "revision":3 if method=="seasonal_intraday_ewma" else 1})
        manifest={"dataset_id":"real-tests","mode":"prospective"}
        normalized,actual=_normalize_ledger(records,outcomes,manifest)
        self.assertEqual(len(actual),1)
        self.assertEqual(actual[0]["realized_variance"],1.)
        revised=copy.deepcopy(outcomes);revised[1]["source_revision"]="corrected-path";revised[1]["observed_variance"]=2.
        _,actual=_normalize_ledger(records,revised,manifest)
        self.assertEqual(actual[0]["status"],"incomplete_data")
        self.assertIsNone(actual[0]["realized_variance"])
        mixed=copy.deepcopy(outcomes);mixed[0]["mode"]="synthetic"
        with self.assertRaisesRegex(ValueError,"dataset/mode"):
            _normalize_ledger(records,mixed,manifest)
        early=copy.deepcopy(outcomes);early[0]["outcome_available_at"]="2024-01-03T21:00:00Z"
        with self.assertRaisesRegex(ValueError,"before full target evidence"):
            _normalize_ledger(records,early,manifest)

    def test_market_date_blocks_preserve_cross_instrument_dependence(self):
        # Opposite losses for two names cancel within every date. Resampling rows
        # independently would produce a spurious nonzero uncertainty interval.
        rows=[{"session":day,"instrument_id":symbol,"delta":sign} for day in ("a","b","c","d") for symbol,sign in (("A",10.),("B",-10.))]
        result=block_uncertainty(rows,"delta",block_length=2,samples=100,seed=123)
        self.assertEqual(result["mean"],0.)
        self.assertEqual(result["lower"],0.)
        self.assertEqual(result["upper"],0.)
        self.assertEqual(result,block_uncertainty(rows,"delta",block_length=2,samples=100,seed=123))

    def test_measurement_uses_actual_path_and_excludes_partial_path(self):
        calendar=pd.DataFrame([{"session":"2024-01-02","open_utc":"2024-01-02T14:30:00Z","close_utc":"2024-01-02T14:40:00Z"}])
        bars=pd.DataFrame([
            {"instrument_id":"A","bar_start":"2024-01-02T14:30:00Z","bar_end":"2024-01-02T14:35:00Z","open":100.,"close":110.,"previous_close_factor":1.},
            {"instrument_id":"A","bar_start":"2024-01-02T14:35:00Z","bar_end":"2024-01-02T14:40:00Z","open":110.,"close":100.,"previous_close_factor":1.},
        ])
        fold={"test_start":"2024-01-02","test_end":"2024-01-02"}
        result=measurement_comparison(bars,calendar,fold)
        self.assertEqual(result["paired_instrument_sessions"],1)
        self.assertGreater(result["rows"][0]["five_minute_rv"],0)
        self.assertAlmostEqual(result["rows"][0]["coarse_hourly_rv"],0.)
        partial=measurement_comparison(bars.iloc[:1],calendar,fold)
        self.assertEqual(partial["coverage_exclusions"][0]["reason"],"incomplete_five_minute_price_path")
        self.assertEqual(measurement_comparison(None,calendar,fold)["status"],"unavailable")


class PacketAcceptanceTests(unittest.TestCase):
    def packet(self):
        summary={"status":"complete","paired_targets":1,
                 "baseline":"seasonal_intraday_ewma","challenger":"intraday_har",
                 "mean_baseline_qlike":1.,"mean_challenger_qlike":.9,
                 "paired_difference_uncertainty":{"samples":20,"mean":-.1,"lower":-.2,"upper":.1},
                 "rows":[{"target_id":"one-paired-target"}]}
        return {"manifest":{"candidates":list(CANDIDATES)},"folds":[{
            "fold":{"fold_id":"test-fold"},
            "candidates":[{"candidate":name,"status":"complete"} for name in CANDIDATES],
            "forecasts":copy.deepcopy(summary),
            "measurement_comparison":{"status":"complete","paired_instrument_sessions":1,
                "rows":[{"instrument_id":"A","session":"2024-01-02","five_minute_rv":.1,"coarse_hourly_rv":.2}],
                "forecast_loss_sensitivity":{"five_minute":copy.deepcopy(summary),"coarse_hourly":copy.deepcopy(summary)}}}]}

    def test_missing_duplicate_or_empty_matrix_cannot_pass_status_only_gate(self):
        packet=self.packet()
        self.assertTrue(packet_completion(packet)["complete"])
        packet["folds"][0]["candidates"].pop()
        self.assertFalse(packet_completion(packet)["complete"])
        packet=self.packet()
        packet["folds"][0]["candidates"][-1]=packet["folds"][0]["candidates"][0]
        self.assertFalse(packet_completion(packet)["complete"])
        packet["folds"]=[]
        self.assertFalse(packet_completion(packet)["complete"])

    def test_actual_measurement_requires_executed_forecast_loss_sensitivity(self):
        packet=self.packet()
        packet["folds"][0]["measurement_comparison"].pop("forecast_loss_sensitivity")
        result=packet_completion(packet)
        self.assertFalse(result["complete"])
        self.assertIn("measurement_common_targets",[f["check"] for f in result["failures"]])

    def test_resampling_and_common_target_identity_are_completion_requirements(self):
        packet=self.packet()
        packet["folds"][0]["forecasts"]["paired_difference_uncertainty"]["samples"]=0
        result=packet_completion(packet)
        self.assertIn("primary_forecasts_uncertainty",[f["check"] for f in result["failures"]])
        packet=self.packet()
        packet["folds"][0]["measurement_comparison"]["forecast_loss_sensitivity"]["coarse_hourly"]["rows"][0]["target_id"]="different-target"
        result=packet_completion(packet)
        self.assertIn("measurement_common_targets",[f["check"] for f in result["failures"]])

    def test_reduced_declared_comparison_remains_a_valid_retained_packet(self):
        packet=self.packet()
        declared=list(CANDIDATES[:2])
        packet["manifest"]["candidates"]=declared
        packet["folds"][0]["candidates"]=packet["folds"][0]["candidates"][:2]
        self.assertFalse(packet_completion(packet)["complete"])
        self.assertTrue(packet_completion(packet,required_candidates=declared)["complete"])
        packet["folds"][0]["candidates"][0]["status"]="failed"
        self.assertFalse(packet_completion(packet,required_candidates=declared)["complete"])
        self.assertEqual(packet["folds"][0]["candidates"][0]["status"],"failed")


if __name__=="__main__":
    unittest.main()
