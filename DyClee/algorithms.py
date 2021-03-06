from copy import deepcopy
from itertools import chain
from collections import deque
import math
from statistics import multimode

import numpy as np
from sklearn.neighbors import KDTree

from .clusters import FinalCluster, MicroCluster
from .utilities import manhattan_distance

# Citation:
# Nathalie Barbosa Roa, Louise Travé-Massuyès, Victor Hugo Grisales.
# DyClee: Dynamic clustering for tracking evolving environments.
# Pattern Recognition, Elsevier, 2019, 94, pp.162-186.
# 10.1016/j.patcog.2019.05.024 . hal-02135580

# Implements a non-parallelized version of the DyClee algorithm, as described
# in the 2019 paper.
class SerialDyClee:
	# All default parameter values are as described in the paper (pages 15-17).
	# @param phi				The only required parameter: hyperbox
	# 							relative-size. Float in range [0, 1].
	# @param forget_method		Forgetting process function (callable).
	# @param ltm				Boolean to activate/deactivate long term
	#							memory behavior.
	# @param unclass_accepted	Boolean to enable (True) / disable (False)
	#							outlier rejection.
	# @param minimum_mc			Boolean to retain (False) all clusters or
	#							discard (True) clusters with few microclusters.
	# @param multi_density		Boolean to apply global density (True) or local
	#							density (False) analysis.
	# @param context			A 2xd NumPy matrix where d is the number of
	#							dimensions, containing the minimum (row 0) and
	#							maximum (row 1) value of each dimension.
	#							Passing the default value of None will activate
	#							adaptive normalization.
	# @param t_global			Integer determining the "Period of the
	#							density-based clustering cycle" (page 17).
	# @param uncdim				Integer specifying the number of dimensions
	#							along which microclusters do not have to
	#							overlap to be considered connected.
	# @param kdtree				Set to True to enable faster connected
	#							micro-cluster search via use of a k-d tree.
	# @param snapshot_alpha		Alpha parameter for pyramidal snapshot
	#							management framework.
	# @param snapshot_l			L parameter for pyramidal snapshot framework.
	# @param var_check			Use variance for deciding in which dimensions
	#							micro-clusters must be overlap to be
	#							considered fully connected. Will only have any
	#							effect if uncdim != 0.
	# @param dense_connect		Only allow connectivity by dense microclusters.
	# @param label_voting		Use mode with voting for tie breaking for
	#							label propagation.
	def __init__(self, phi, forget_method=None, ltm=False,
		unclass_accepted=True, minimum_mc=False, multi_density=False,
		context=None, t_global=1, uncdim=0, kdtree=False, snapshot_alpha=2,
		snapshot_l=2, var_check=False, dense_connect=False,
		label_voting=False):
		assert phi >= 0 and phi <= 1, "Invalid phi given"
		if phi > 0.5:
			print("Warning: relative size (phi) > 0.5 may yield poor results")

		# Algorithm customization
		self.phi = phi
		self.forget_method = forget_method
		self.ltm = ltm
		self.unclass_accepted = unclass_accepted
		self.minimum_mc = minimum_mc
		self.density_stage = self._density_stage_local if multi_density else \
			self._density_stage_global

		if context is None:
			self.context = None
			self.norm_func = self._adaptive_normalize_and_update
		else: # Append max-min row for reduced calculations
			#self.context = np.append(context, (context[1] - context[0]),
			#	axis=0)
			self.context = np.vstack([context, (context[1] - context[0])])
			self.norm_func = self._normalize

		self.t_global = t_global
		self.uncdim = uncdim
		self.common_dims = context.shape[1] - uncdim if context is not None \
			else None # d - uncdim
		self.use_kdtree = kdtree
		if kdtree:
			self.spatial_search = self._search_kdtree
			self.kdtree = None
			self.center_map = None
		else:
			self.spatial_search = self._search_all_clusters
		self.snapshot_alpha = snapshot_alpha
		self.snapshot_l = snapshot_l
		self.max_snapshots = (snapshot_alpha ** snapshot_l) + 1

		# Storage
		self.A_list = [] # Active medium and high density microclusters
		self.O_list = [] # Low density microclusters
		self.long_term_mem = [] # All microclusters once dense, now low density
		self.snapshots = {} # Need to define how this is maintained

		# Dimensionality reduction 
		self.var_check = var_check
		self.variances = np.zeros((1, context.shape[1]),
			dtype=np.float64) if context is not None else None

		self.density_check = self._is_dense if dense_connect else \
			self._is_not_outlier
		self.label_voting = label_voting

		# Other
		self.hyperbox_sizes = self._get_hyperbox_sizes() if context is not \
			None else None
		self.hyperbox_volume = self._get_hyperbox_volume() if context is not \
			None else None

		self.id_prefix = "dyclee" + str(np.random.default_rng().integers(
			1000)) + "_"
		self.next_class_id = 0

	# Helper to calculate microcluster hyperbox sizes along each dimension.
	# Size = phi * |dmax - dmin|
	def _get_hyperbox_sizes(self):
		return self.phi * np.ones(self.context.shape[1], dtype=np.float64)


	# Helper to calculate the current microcluster hyperbox volume.
		# Vol = product of all sizes
	def _get_hyperbox_volume(self):
		return np.prod(self._get_hyperbox_sizes())


	def _get_next_class_id(self):
		temp = self.id_prefix + str(self.next_class_id)
		self.next_class_id += 1
		return temp


	def _is_dense(self, uC):
		return uC.density_type == "Dense"


	def _is_not_outlier(self, uC):
		return uC.density_type != "Outlier"


	# Normalization function for use in cases of no context matrix provided;
	# also, must update the hypervolumes of all microclusters.
	def _adaptive_normalize_and_update(self, X):
		diffmin = X - self.context[0]
		diffmax = X - self.context[1]
		diffminbool = diffmin < 0
		diffmaxbool = diffmax > 0
		updatemins = np.any(diffminbool)
		updatemaxs = np.any(diffmaxbool)
		if updatemins:
			self.context[0] = self.context[0] + np.where(diffminbool,
				diffmin, 0)
		if updatemaxs:
			self.context[1] = self.context[1] + np.where(diffmaxbool,
				diffmax, 0)

		# Update differences
		self.context[2] = self.context[1] - self.context[0]

		# Update hyperbox statistics
		self.hyperbox_sizes = self._get_hyperbox_sizes()
		self.hyperbox_volume = self._get_hyperbox_volume()

		# Need to re-normalize all existing microclusters and return normalized
		# point

	# Normalization function for use in case of passed context matrix.
	def _normalize(self, X):
		return ((X - self.context[0])/(self.context[2]))


	# Helper function to determine if a microcluster is reachable from new
	# instance X.
	def _is_reachable(self, microcluster, X):
		diff = np.abs(X - microcluster.center)
		halvedsize = self.hyperbox_sizes / 2
		return np.all(diff < halvedsize)


	# Helper function to find all reachable microclusters in a given list
	def _find_reachables(self, curr_list, X):
		return [uC for uC in curr_list if self._is_reachable(uC, X)]


	# Helper function to determine if two microclusters are connected.
	# Implements definition 3.2 (page 7) - doesn't insist on a particular
	# subset of features, only that a subset could be created to satisfy
	# overlap among (d - uncdim) dimensions.
	def _is_connected(self, microclusterA, microclusterB):
		diff = np.abs(microclusterA.center - microclusterB.center)
		halvedsize = self.hyperbox_sizes
		numoverlapdims = 0

		# Dimensionality reduction
		if self.var_check:
			feature_selection = self.variances.argsort().flatten()[
				-(self.common_dims):]
			for i in range(len(diff)):
				if i in feature_selection:
					if diff[i] < halvedsize[i]:
						numoverlapdims += 1
		else:
			numoverlapdims = np.sum((diff < halvedsize).astype(np.uint8))
		if numoverlapdims >= self.common_dims: # Check for sufficient overlap
			return True
		else:
			return False


	# Helper function to find all neighbors in the density stage by searching
	# all clusters.
	def _search_all_clusters(self, curruC):
		return [uC for uC in chain(self.A_list, self.O_list) if \
			self._is_connected(uC, curruC)]


	# Helper function to construct KDTree and center map dictionary, which
	# references microclusters based on their center.
	def _construct_kdtree(self):
		self.center_map = {}
		data = []
		for uC in chain(self.A_list, self.O_list):
			center = uC.center
			self.center_map[str(center)] = uC
			data.append(center)
		data = np.vstack(data)
		self.kdtree = KDTree(data, metric="manhattan")


	# Helper function to find all neighbors in the density stage by using a
	# kdtree.
	def _search_kdtree(self, curruC):
		match_indices = self.kdtree.query_radius(curruC.center,
			self.phi / 2)[0]
		data = self.kdtree.get_arrays()[0]
		return [self.center_map[str(arr)] for arr in data[match_indices]]


	# Helper function to manage final cluster snapshot history
	def _update_snapshots(self, tX, clusters):
		if tX == 0:
			max_order = 0
		else:
			max_order = math.floor(math.log(tX, self.snapshot_alpha))
		for order in range(max_order + 1):
			if tX % (self.snapshot_alpha ** order) == 0:
				if order not in self.snapshots:
					self.snapshots[order] = {tX : clusters}
				else:
					self.snapshots[order][tX] = clusters
					tstamps = list(self.snapshots[order].keys())
					tstamps.sort()
					if len(self.snapshots[order]) > self.max_snapshots:
						del self.snapshots[order][tstamps[0]]
		for order in range(len(self.snapshots)):
			for k in list(self.snapshots[order].keys()):
				if k % (self.snapshot_alpha ** (order + 1)) == 0:
					del self.snapshots[order][k]


	# Helper function for finding the best neighbor microcluster for insertion
	# in a list of microclusters by distance, breaking ties based on density.
	def _find_best_neighbor(self, neighbor_list, X):
		closest = []
		min_dist = np.inf

		for uC in neighbor_list:
			dist = manhattan_distance(uC.center, X)
			if dist < min_dist:
				closest = [uC]
				min_dist = dist
			elif dist == min_dist: # Ties
				closest.append(uC)

		# Sort by density to break ties among equally close
		# microclusters
		closest.sort(key = lambda uC : uC.Dk)
		return closest[0]

	# Implements algorithm 1, distance stage (page 6).
	def _distance_stage(self, X, tX, X_class=None):
		# No microclusters ever created
		if len(self.A_list) == 0 and len(self.O_list) == 0 and \
			len(self.long_term_mem) == 0:
			# If doing adaptive normalization, cannot expect valid density
			# after only one insertion.
			volume = self.hyperbox_volume if self.hyperbox_volume is not None \
				else 1
			new_uC = MicroCluster(X, tX, volume, X_class, self.forget_method)
			self.O_list.append(new_uC)
			return new_uC
		else: # First check A-list
			Reachables = self._find_reachables(self.A_list, X)
			if len(Reachables) != 0: # If there are reachable microclusters
				best_match = self._find_best_neighbor(Reachables, X)
				best_match.insert(X, tX, X_class)
				return best_match
			else: # Then check O-list
				Reachables = self._find_reachables(self.O_list, X)
				if len(Reachables) != 0: # If there are reachable microclusters
					# Find closest uC in Reachables
					best_match = self._find_best_neighbor(Reachables, X)
					best_match.insert(X, tX, X_class)
					return best_match
				else: # Check long term memory
					Reachables = self._find_reachables(self.long_term_mem, X)
					if self.ltm and len(Reachables) != 0:
						best_match = self._find_best_neighbor(Reachables, X)
						resurrected = deepcopy(best_match)
						resurrected.insert(X, tX, X_class)
						self.O_list.append(resurrected)
						return resurrected
					else: # Create uC with X info
						volume = self.hyperbox_volume if self.hyperbox_volume \
							is not None else 1
						new_uC = MicroCluster(X, tX, volume, X_class,
							self.forget_method)
						self.O_list.append(new_uC)
						return new_uC

	# Returns average and median density of given list of clusters.
	def _get_avg_med_density(self, clist):
		cla = np.array([uC.Dk for uC in clist], dtype=np.float64)
		return np.mean(cla), np.median(cla)

	# Given a list of micro-clusters contributing to a final cluster, returns
	# the label, projected center, density and max distance (a measure of
	# spread) of the final cluster
	def _calculate_final_cluster(self, fclist):
		num_contrib_mc = len(fclist)
		center = np.zeros((fclist[0].center.shape),
			dtype=np.float64)
		avg_density = 0
		max_distance = 0
		label = fclist[0].Classk

		for uC in fclist:
			center += uC.center
			avg_density += uC.Dk

		# Take averages and find max distance
		center /= num_contrib_mc
		avg_density /= num_contrib_mc
		for uC in fclist:
			dist = manhattan_distance(center, uC.center)
			if dist > max_distance:
				max_distance = dist

		return label, center, avg_density, max_distance

	# Implements local density analysis stage.
	def _density_stage_local(self, tX):
		pass

	# Performs density analysis on a given list of MicroClusters.
	# @param	uClist	A list of MicroClusters to analyze.
	# @param	d_avg	The average density of the MicroClusters.
	# @param 	d_med	The median density of the MicroClusters.
	# @return			Returns lists of the Dense, Semi-Dense and Low-Density
	#					MicroClusters.
	def _density_analysis(self, uClist, d_avg, d_med):
		DMC, SDMC, LDMC = [],[],[] # Dense, Semi-Dense, Low-Density
		for uC in uClist:
			if uC.Dk >= d_avg and uC.Dk >= d_med:
				uC.set_density_type("Dense")
				# Organize dense clusters so as to prioritize classed dense
				# clusters as seeds first to avoid unnecessary label
				# generation and re-labeling
				if uC.Classk != 'Unclassed':
					DMC.insert(0, uC)
				else:
					DMC.append(uC)
			elif uC.Dk >= d_avg or uC.Dk >= d_med:
				uC.set_density_type("Semi-Dense")
				SDMC.append(uC)
			else:
				uC.set_density_type("Low-Density")
				LDMC.append(uC)

		return DMC, SDMC, LDMC

	# Implements algorithm 2, density stage - global analysis.
	# Returns updated A-list and O-list
	def _density_stage_global(self, tX):
		g_avg, g_med = self._get_avg_med_density(chain(self.A_list,
			self.O_list))
		DMC, SDMC, LDMC = self._density_analysis(chain(self.A_list,
			self.O_list), g_avg, g_med)
		already_seen = set()
		final_clusters = []

		if self.use_kdtree:
			self._construct_kdtree()

		for uC in DMC:
			if uC not in already_seen:
				already_seen.add(uC)
				## May need to change this to class assignment upon final
				## cluster formation.
				if uC.Classk == 'Unclassed':
					label = self._get_next_class_id()
					uC.Classk = label
				else:
					label = uC.Classk
				final_cluster = [uC] # Create "final cluster"
				Connected_uC = deque(self.spatial_search(uC))

				# Optional voting behavior
				if self.label_voting:
					labels = [label]
					final_label = None
					classed = [uC]
					outliers = []
					while len(Connected_uC) != 0:
						uCneighbor = Connected_uC.popleft()
						if self._is_not_outlier(uCneighbor) and \
							uCneighbor not in already_seen:
							curr_class = uCneighbor.Classk
							if curr_class != "Unclassed":
								labels.append(curr_class)
								classed.append(uCneighbor)
								if not curr_class.startswith(self.id_prefix):
									final_label = curr_class
							already_seen.add(uCneighbor)
							final_cluster.append(uCneighbor)
							NewConnected_uC = self.spatial_search(uCneighbor)
							for newneighbor in NewConnected_uC:
								if self._is_not_outlier(uCneighbor) and \
									newneighbor not in already_seen:
									Connected_uC.append(newneighbor)
									curr_class = newneighbor.Classk
									if curr_class != "Unclassed":
										labels.append(newneighbor.Classk)
										classed.append(newneighbor)
										if not curr_class.startswith(
											self.id_prefix):
											final_label = curr_class
								elif newneighbor not in already_seen:
									outliers.append(newneighbor)
									already_seen.add(newneighbor)

					_, center, avg_density, max_distance = \
						self._calculate_final_cluster(final_cluster)

					# Label voting
					if final_label is None:
						final_label = multimode(labels)
						if len(final_label) == 1:
							final_label = final_label[0] # Extract single mode
						elif len(final_label) == len(final_cluster):
							final_label = label
						else: # Multiple modes
							votes = {k : 0 for k in labels}
							for uC in classed:
								curr_class = uC.Classk
								if curr_class in votes:
									# Votes are weighted by uC density and its
									# distance from the final cluster center
									votes[curr_class] += (uC.Dk * (1 /
										manhattan_distance(uC.center, center)))
							final_label = max(votes, key=lambda x : votes[x])

					# Apply labels
					for uC in chain(outliers, final_cluster):
						uC.Classk = final_label

					final_clusters.append(FinalCluster(final_label, center,
						avg_density, max_distance))

				# Default behavior
				else:
					while len(Connected_uC) != 0:
						uCneighbor = Connected_uC.popleft()
						if self.density_check(uCneighbor) and \
							uCneighbor not in already_seen:
							uCneighbor.Classk = label
							already_seen.add(uCneighbor)
							final_cluster.append(uCneighbor)
							NewConnected_uC = self.spatial_search(uCneighbor)
							for newneighbor in NewConnected_uC:
								if self.density_check(newneighbor) and \
									newneighbor not in already_seen:
									Connected_uC.append(newneighbor)
								newneighbor.Classk = label

					# Calculate and store final cluster
					label, center, avg_density, max_distance = \
						self._calculate_final_cluster(final_cluster)
					final_clusters.append(FinalCluster(label, center,
						avg_density, max_distance))

		# Process snapshots
		snap_copies = []
		if DMC is not None:
			snap_copies.extend(deepcopy(DMC))
		if SDMC is not None:
			snap_copies.extend(deepcopy(SDMC))
		if LDMC is not None:
			snap_copies.extend(deepcopy(LDMC))
		self._update_snapshots(tX, {'final': final_clusters,
			'all': snap_copies})

		# Return "message" of updated lists
		new_A_list = DMC + SDMC # All current dense and semi-dense
		long_term_mem_additional = []

		if self.forget_method is None: # No forgetting process
			new_O_list = LDMC
		else:
			for uC in LDMC:
				# Check if meets low density threshold (.25 of the global
				# density average), or was created recently (to avoid deleting
				# new growing low-density micro-clusters), for O-list inclusion
				if (uC.Dk > 0.25 * g_avg and uC.Dk > 0.25 * g_med) or \
					(tX - uC.tlk) <= 10:
					new_O_list.append(uC)
				elif uC.was_dense and self.ltm: # Store in long term memory
					long_term_mem_additional.append(uC)

		return new_A_list, new_O_list, long_term_mem_additional


	# Runs the DyClee algorithm on a set part of a finite dataset.
	# NOT PART OF THE ORIGINAL DYCLEE ALGORITHM. Designed to avoid running the
	# density stage before reasonable mins and maxs have been observed.
	# @param iterations		The number of instances to process.
	def warm_start(self, iterations):
		pass


	# Initializes parameters and returns a time column, target column and
	# a sum of squares vector.
	def _initialize(self, num_instances, num_features, timecol, targetcol):
		if self.common_dims is None: # Initialize common dimensions
			self.common_dims = num_features - self.uncdim

		if self.context is None: # Initialize context matrix
			self.context = np.zeros((3, num_features), dtype=np.float64)
			self.variances = np.zeros((1, num_features), dtype=np.float64)

		# Indices if not time-series data
		tcol = np.arange(num_instances) if timecol is None else timecol

		# All Unclassed if unsupervised
		tgcol = np.array(["Unclassed"] * num_instances) if targetcol is None \
			else targetcol

		ssq = np.zeros(num_features, dtype=np.float64)

		return tcol, tgcol, ssq


	# Runs the DyClee algorithm on a finite dataset using episode abstraction.
	def run_dataset_w_abstraction(self, data, timecol=None, targetcol=None,
		max_window=None):
		num_instances, num_features = data.shape
		timecol, targetcol, total_SS = self._initialize(num_instances,
			num_features, timecol, targetcol)
		mw = math.floor(num_instances/2) if max_window is None else max_window

		# Primary loop
		clustering_results=[]
		count_since_last_density = 0
		complete = False
		current_loc = 0
		while not complete:
			# Find suitable window to fit polynomial of degree 2
			curr_window_length = mw
			while curr_window_length > 1:
				current_bound = current_loc + curr_window_length
				totalvar = np.sum(np.var(
					data[current_loc:current_bound], axis=0))
				coefs, residuals, rank, singular_values, rcond = np.polyfit(
					timecol[current_loc:current_bound],
					data[current_loc:current_bound], 2, full=True)
				totalres = np.sum(residuals)
				if totalres > totalvar:
					curr_window_length = math.floor(curr_window_length / 2)
				else:
					break

			# Insert coefficients as instance
			coefs, residuals, rank, singular_values, rcond = np.polyfit(
					timecol[current_loc:current_bound],
					data[current_loc:current_bound], 2, full=True)

			#inserteduC = self._distance_stage(, tX, X_class)

			# UPDATE current_loc



	# Runs the DyClee algorithm on a finite dataset.
	# @param data		Data matrix where each row is an instance, each
	#					column is for an attribute.
	# @param timecol	Time increments of the data if available; default of 
	#					None disables time behavior. Forget_method should also
	#					be None.
	# @param targetcol	Column of labels. Absence of a label MUST be indicated
	#					with None in that row.
	# @return			Returns an array of labels corresponding to the input
	#					instances (in their input order). For a given point
	#					this is the last label the microcluster into which it
	#					was inserted took on.
	def run_dataset(self, data, timecol=None, targetcol=None):
		num_instances, num_features = data.shape
		timecol, targetcol, total_SS = self._initialize(num_instances,
			num_features, timecol, targetcol)

		# Primary loop
		clustering_results=[]
		count_since_last_density = 0
		for i in range(num_instances):
			X = self.norm_func(data[i]) # Normalize data
			tX = timecol[i]
			X_class = targetcol[i]

			# Variance calculations
			if self.var_check:
				total_SS += np.power(X, 2) # Update sum of squares
				self.variances = total_SS/(i+1) # Current variances

			# Run distance stage - append MicroCluster reference to results
			clustering_results.append(self._distance_stage(X, tX, X_class))

			# Decay all microclusters
			for uC in chain(self.A_list, self.O_list):
				uC.update_cluster(tX)
				uC.update_density(self.hyperbox_volume)

			# Run density stage
			count_since_last_density += 1
			if count_since_last_density == self.t_global:
				count_since_last_density = 0
				new_A, new_O, long_term_mem_additional = self.density_stage(tX)
				self.A_list = new_A
				self.O_list = new_O
				self.long_term_mem.extend(long_term_mem_additional)

		return np.array([uC.Classk for uC in clustering_results])

	# Runs the DyClee algorithm on streaming data.
	def run_datastream(self, ):
		# MUST INITIALIZE self.common_dims and self.context
		pass
